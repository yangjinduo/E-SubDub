# -*- coding: utf-8 -*-
"""流程编排引擎：QThread 中按阶段串行执行流水线，支持人工审核暂停点。

阶段顺序（配音模式全量；仅字幕模式跳过人声分离 / TTS / 混音 / 封装）：
音频提取 → 人声分离 → ASR → LLM 校对 → [ASR 人工审核] → LLM 翻译 →
[译文人工审核] → SRT 字幕 → 参考音色挑选/[音色管理] → TTS → [异常复听] →
混音 → MKV 封装。

暂停机制：runner 在工作线程发射 review_requested 后阻塞在 threading.Event，
UI 线程弹对话框并调用 submit_review() 回传结果；回传 None / accepted=False
视为取消，已完成阶段的产物保留（manifest 记录），下次运行自动复用。
"""

from __future__ import annotations

import logging
import shutil
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.i18n import tr
from app.core.segment import Segment
from app.core.workspace import (
    STAGE_ASR,
    STAGE_EXTRACT,
    STAGE_MIX,
    STAGE_MUX,
    STAGE_PROOFREAD,
    STAGE_REVIEW,
    STAGE_REVIEW_TRANSLATION,
    STAGE_SEPARATE,
    STAGE_TRANSLATE,
    STAGE_TTS,
    Workspace,
)
from app.pipeline.audio_extract import VIDEO_EXTS, extract_audio
from app.pipeline.base import ProgressCallback
from app.pipeline.subtitle import (
    MODE_BILINGUAL_SOURCE_TOP,
    MODE_BILINGUAL_TARGET_TOP,
    MODE_MONO,
    generate_srt,
)

logger = logging.getLogger(__name__)

# 内容语言代码 → LLM 提示词用的语言名（固定中文，与界面语言无关）
LANG_NAMES = {
    "zh": "中文", "en": "英语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "ru": "俄语",
}

# 内容语言代码 → ffmpeg MKV 元数据用的 ISO 639-2 代码
FFMPEG_LANGS = {
    "zh": "chi", "en": "eng", "ja": "jpn", "ko": "kor",
    "fr": "fre", "de": "ger", "es": "spa", "ru": "rus",
}

# 字幕形式：task_settings 代码 → subtitle.py 模式
SUBTITLE_MODE_MAP = {
    "single": MODE_MONO,
    "bilingual_target": MODE_BILINGUAL_TARGET_TOP,
    "bilingual_source": MODE_BILINGUAL_SOURCE_TOP,
}


class _PipelineCancelled(Exception):
    """用户取消流水线（在各阶段边界与进度回调中抛出）。"""


@dataclass
class TaskConfig:
    """一次任务的完整配置（主窗口校验资源后构造）。"""

    media_path: str
    output_dir: str
    source_lang_code: str          # "auto"/"zh"/"en"…
    target_lang_code: str          # "zh"/"en"…
    subtitle_mode: str             # task_settings 的 SUBTITLE_* 代码
    dubbing_enabled: bool
    device: str                    # "cuda"/"mps"/"cpu"
    task_category: str = ""        # 任务类别（工作夹名后缀，固定中文）
    auto_accept_reviews: bool = False  # 自动通过人工审核（挂机批量）
    asr_model_dir: str = ""
    tts_model_dir: str = ""
    llm_model_path: str = ""       # GGUF 文件路径
    llm_server_exe: str = ""       # llama-server 可执行文件路径


@dataclass
class ReviewPayload:
    """审核暂停请求（review_requested 信号负载）。

    kind: "asr" / "translation" / "voice" / "tts"
    segments: 对话框就地编辑的 Segment 列表
    audio_wav: 试听音轨路径
    ref_map: voice 阶段的 {spk: 参考音频路径}
    source_lang / target_lang: translation 阶段的界面语言名
    """

    kind: str
    segments: list = field(default_factory=list)
    audio_wav: str = ""
    ref_map: dict = field(default_factory=dict)
    source_lang: str = ""
    target_lang: str = ""


@dataclass
class ReviewResult:
    """审核结果（UI 线程通过 submit_review 回传）。"""

    accepted: bool
    ref_map: dict = field(default_factory=dict)      # voice：确认后的映射
    regenerate_indices: list = field(default_factory=list)  # tts：需重生成的索引


@dataclass
class RunResult:
    """全部完成后的产物信息（finished_ok 信号负载）。"""

    srt_path: str
    mkv_path: str          # 音频输入（无视频可封装）时为空串
    audio_path: str        # 混音成品（仅字幕模式为空串）
    workspace: Workspace


class PipelineRunner(QThread):
    """流水线编排线程。

    信号：
        step_state_changed(str, str): 步骤状态变化（step_id, active/done）
        progress_changed(int): 总进度 0-100
        log_emitted(str): 追加日志
        review_requested(object): 请求人工审核（ReviewPayload）
        finished_ok(object): 全部完成（RunResult）
        failed(str): 出错（错误消息）
        cancelled(): 用户取消
    """

    step_state_changed = Signal(str, str)
    progress_changed = Signal(int)
    log_emitted = Signal(str)
    review_requested = Signal(object)
    finished_ok = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(self, config: TaskConfig, workspace: Workspace | None = None, parent=None) -> None:
        super().__init__(parent)
        self._cfg = config
        # 优先复用外部传入的 Workspace：与 TaskManager 持同一实例，
        # manifest 完成标记在内存中即时可见（否则 UI 侧副本会过期）
        self._ws = workspace or Workspace(
            config.output_dir, config.media_path, category=config.task_category
        )
        self._cancel = threading.Event()
        self._review_event = threading.Event()
        self._review_result: ReviewResult | None = None
        # 进度权重（按激活阶段动态求和）
        self._weights: list[tuple[str, float]] = []
        self._progress_base = 0.0
        self._last_stage = ""
        self._last_log_message = ""

    # ------------------------------------------------------------------ 对外

    def submit_review(self, result: ReviewResult | None) -> None:
        """UI 线程回传审核结果；None 或 accepted=False 取消流水线。"""
        self._review_result = result
        self._review_event.set()

    def cancel(self) -> None:
        """请求取消（在下一个检查点生效）。"""
        self._cancel.set()
        self._review_event.set()  # 唤醒可能正在等待的审核暂停

    @property
    def workspace(self) -> Workspace:
        return self._ws

    # ------------------------------------------------------------------ 线程体

    def run(self) -> None:  # noqa: C901 - 主流程，分支为各阶段
        try:
            result = self._execute()
            self.finished_ok.emit(result)
        except _PipelineCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 统一上报 UI
            logger.exception("流水线执行失败")
            self.failed.emit(f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 主流程

    def _execute(self) -> RunResult:
        cfg = self._cfg
        dub = cfg.dubbing_enabled
        is_video = Path(cfg.media_path).suffix.lower() in VIDEO_EXTS
        translate_needed = cfg.source_lang_code != cfg.target_lang_code or cfg.source_lang_code == "auto"

        self._log(tr("runner.started", name=Path(cfg.media_path).name))
        self._build_weights(dub, is_video, translate_needed)

        # 1. 音频提取
        self._stage_begin(STAGE_EXTRACT)
        if self._ws.is_done(STAGE_EXTRACT):
            self._skip_stage(STAGE_EXTRACT)
        else:
            extract_audio(
                cfg.media_path, self._ws.ensure_stage_dir(STAGE_EXTRACT),
                progress=self._stage_progress(STAGE_EXTRACT),
            )
            self._ws.mark_done(STAGE_EXTRACT)
            self._stage_end(STAGE_EXTRACT)
        full_audio = str(self._ws.full_audio)
        self._check()

        # 2. 人声分离（仅配音模式）
        vocals = full_audio
        no_vocals = ""
        if dub:
            self._stage_begin(STAGE_SEPARATE)
            if self._ws.is_done(STAGE_SEPARATE):
                self._skip_stage(STAGE_SEPARATE)
            else:
                from app.pipeline.separate import separate_vocals
                try:
                    separate_vocals(
                        full_audio, self._ws.ensure_stage_dir(STAGE_SEPARATE),
                        device=cfg.device,
                        progress=self._stage_progress(STAGE_SEPARATE),
                    )
                except Exception as exc:
                    if cfg.device == "cpu":
                        raise
                    self._log(tr("runner.device_fallback", stage=tr("step.separate"), error=exc))
                    separate_vocals(
                        full_audio, self._ws.ensure_stage_dir(STAGE_SEPARATE),
                        device="cpu",
                        progress=self._stage_progress(STAGE_SEPARATE),
                    )
                self._ws.mark_done(STAGE_SEPARATE)
                self._stage_end(STAGE_SEPARATE)
            vocals = str(self._ws.vocals)
            no_vocals = str(self._ws.no_vocals)
            self._check()

        # 3. ASR 转写
        self._stage_begin(STAGE_ASR)
        if self._ws.is_done(STAGE_ASR):
            segments = self._ws.load_segments(self._ws.asr_json)
            self._skip_stage(STAGE_ASR)
        else:
            segments = self._run_asr(vocals)
            self._ws.save_segments(self._ws.asr_json, segments)
            self._ws.mark_done(STAGE_ASR)
            self._stage_end(STAGE_ASR)
        self._check()

        # 4. LLM 校对
        self._stage_begin(STAGE_PROOFREAD)
        if self._ws.is_done(STAGE_PROOFREAD):
            segments = self._ws.load_segments(self._ws.corrected_pre)
            self._skip_stage(STAGE_PROOFREAD)
        else:
            with self._llm_session() as client:
                from app.pipeline.proofread import proofread_segments
                segments = proofread_segments(
                    client, segments, progress=self._stage_progress(STAGE_PROOFREAD)
                )
            self._ws.save_segments(self._ws.corrected_pre, segments)
            self._ws.mark_done(STAGE_PROOFREAD)
            self._stage_end(STAGE_PROOFREAD)
        self._check()

        # 5. ASR 人工审核
        self._stage_begin(STAGE_REVIEW)
        if self._ws.is_done(STAGE_REVIEW):
            segments = self._ws.load_segments(self._ws.corrected_post)
            self._skip_stage(STAGE_REVIEW)
        else:
            pre_text = self._read_text(self._ws.corrected_pre)
            self._request_review(ReviewPayload(
                kind="asr", segments=segments, audio_wav=vocals,
            ))
            self._ws.save_segments(self._ws.corrected_post, segments)
            if self._read_text(self._ws.corrected_post) != pre_text:
                # 人工修改过内容：下游翻译及其后全部失效重跑
                self._ws.reset_stages([
                    STAGE_TRANSLATE, STAGE_REVIEW_TRANSLATION, STAGE_TTS, STAGE_MIX, STAGE_MUX,
                ])
            self._ws.mark_done(STAGE_REVIEW)
            self._stage_end(STAGE_REVIEW)
        self._check()

        # 6. LLM 翻译（源=目标语言时直接复用原文）
        self._stage_begin(STAGE_TRANSLATE)
        if not translate_needed:
            for seg in segments:
                if not seg.target_text:
                    seg.target_text = seg.text
            self._ws.save_segments(self._ws.translated_pre, segments)
            self._ws.mark_done(STAGE_TRANSLATE)
            self._stage_end(STAGE_TRANSLATE)
        elif self._ws.is_done(STAGE_TRANSLATE):
            segments = self._ws.load_segments(self._ws.translated_pre)
            self._skip_stage(STAGE_TRANSLATE)
        else:
            with self._llm_session() as client:
                from app.pipeline.translate import translate_segments
                segments = translate_segments(
                    client, segments,
                    LANG_NAMES.get(cfg.source_lang_code, "原文语言"),
                    LANG_NAMES.get(cfg.target_lang_code, "目标语言"),
                    progress=self._stage_progress(STAGE_TRANSLATE),
                )
            self._ws.save_segments(self._ws.translated_pre, segments)
            self._ws.mark_done(STAGE_TRANSLATE)
            self._stage_end(STAGE_TRANSLATE)
        self._check()

        # 7. 译文人工审核
        self._stage_begin(STAGE_REVIEW_TRANSLATION)
        if self._ws.is_done(STAGE_REVIEW_TRANSLATION):
            segments = self._ws.load_segments(self._ws.translated_post)
            self._skip_stage(STAGE_REVIEW_TRANSLATION)
        else:
            pre_text = self._read_text(self._ws.translated_pre)
            self._request_review(ReviewPayload(
                kind="translation", segments=segments, audio_wav=vocals,
                source_lang=LANG_NAMES.get(cfg.source_lang_code, ""),
                target_lang=LANG_NAMES.get(cfg.target_lang_code, ""),
            ))
            self._ws.save_segments(self._ws.translated_post, segments)
            if self._read_text(self._ws.translated_post) != pre_text:
                self._ws.reset_stages([STAGE_TTS, STAGE_MIX, STAGE_MUX])
            self._ws.mark_done(STAGE_REVIEW_TRANSLATION)
            self._stage_end(STAGE_REVIEW_TRANSLATION)
        self._check()

        # 8. SRT 字幕（文件名随字幕形式：单语仅目标语种代号，双语上位语言在前）
        srt_mode = SUBTITLE_MODE_MAP.get(cfg.subtitle_mode, MODE_MONO)
        srt_path = self._ws.srt_path(
            cfg.target_lang_code, cfg.source_lang_code,
            source_on_top=(srt_mode == MODE_BILINGUAL_SOURCE_TOP),
        )
        generate_srt(
            segments, str(srt_path), srt_mode,
            source_lang=cfg.source_lang_code, target_lang=cfg.target_lang_code,
        )
        self._log(tr("runner.srt_done", path=srt_path.name, count=len(segments)))

        # 9+. 配音链路
        audio_out = ""
        mkv_out = ""
        if dub:
            # 9. 参考音色挑选 + 音色管理审核
            refs = self._pick_and_review_voices(segments, vocals)

            # 10. TTS 逐句生成 + 异常复听
            self._stage_begin(STAGE_TTS)
            if self._ws.is_done(STAGE_TTS):
                segments = self._ws.load_segments(self._ws.tts_json)
                self._skip_stage(STAGE_TTS)
            else:
                self._run_tts(segments, refs, vocals)
            self._check()

            # 11. 混音
            self._stage_begin(STAGE_MIX)
            if self._ws.is_done(STAGE_MIX):
                self._skip_stage(STAGE_MIX)
            else:
                from app.pipeline.mix import mix_tracks
                self._ws.ensure_stage_dir(STAGE_MIX)
                mix_tracks(
                    segments, no_vocals, str(self._ws.mixed_audio), vocals,
                    progress=self._stage_progress(STAGE_MIX),
                )
                self._ws.mark_done(STAGE_MIX)
                self._stage_end(STAGE_MIX)
            audio_out = str(self._ws.mixed_audio)
            self._check()

            # 12. MKV 封装（音频输入无视频流可封装，跳过）
            if is_video:
                self._stage_begin(STAGE_MUX)
                if self._ws.is_done(STAGE_MUX):
                    self._skip_stage(STAGE_MUX)
                else:
                    from app.pipeline.mux import mux_mkv
                    mux_dir = self._ws.ensure_stage_dir(STAGE_MUX)
                    mux_mkv(
                        cfg.media_path, audio_out, str(srt_path),
                        str(self._ws.output_mkv),
                        audio_lang=FFMPEG_LANGS.get(cfg.target_lang_code, "und"),
                        subtitle_lang=FFMPEG_LANGS.get(cfg.target_lang_code, "und"),
                        progress=self._stage_progress(STAGE_MUX),
                    )
                    # 封装输出夹内同时保留处理好的字幕文件副本
                    shutil.copy2(srt_path, mux_dir / srt_path.name)
                    self._ws.mark_done(STAGE_MUX)
                    self._stage_end(STAGE_MUX)
                mkv_out = str(self._ws.output_mkv)
            else:
                # 音频输入无视频流可封装：混音成品复制到工作夹根目录
                shutil.copy2(self._ws.mixed_audio, self._ws.dubbed_wav)
                audio_out = str(self._ws.dubbed_wav)
                self._log(tr("runner.mux_skipped_audio_input"))

        self.progress_changed.emit(100)
        return RunResult(
            srt_path=str(srt_path), mkv_path=mkv_out, audio_path=audio_out,
            workspace=self._ws,
        )

    # ------------------------------------------------------------------ 阶段实现

    def _run_asr(self, wav_path: str) -> list[Segment]:
        """ASR 转写（设备加载失败自动回退 CPU 重试一次）。"""
        from app.pipeline.asr import ASRModule

        cfg = self._cfg
        progress = self._stage_progress(STAGE_ASR)
        module = ASRModule()
        device = cfg.device
        try:
            module.load(cfg.asr_model_dir, device=device, progress=progress)
            segments = module.transcribe(wav_path, progress=progress)
        except Exception as exc:
            module.unload()
            if device == "cpu":
                raise
            self._log(tr("runner.device_fallback", stage=tr("step.asr"), error=exc))
            device = "cpu"
            module.load(cfg.asr_model_dir, device=device, progress=progress)
            segments = module.transcribe(wav_path, progress=progress)
        finally:
            module.unload()
        return segments

    def _pick_and_review_voices(self, segments: list, vocals: str) -> dict:
        """挑选参考音色并弹音色管理审核，返回 {spk: ref_wav}。"""
        from app.pipeline.voice_pick import pick_reference_segments

        if self._ws.is_done("voice"):
            # 复用此前确认的参考音色目录
            refs = {
                p.stem.replace("ref_", ""): str(p)
                for p in self._ws.ref_dir.glob("ref_*.wav")
            }
            if refs:
                self._log(tr("runner.voice_reused"))
                return refs

        refs = pick_reference_segments(
            segments, vocals, self._ws.ref_dir,
            progress=self._null_progress(),
        )
        if not refs:
            raise RuntimeError(tr("runner.voice_none"))
        self._log(tr("runner.voice_picked", count=len(refs)))

        result = self._request_review(ReviewPayload(
            kind="voice", segments=segments, audio_wav=vocals, ref_map=refs,
        ))
        refs = result.ref_map or refs
        # 把确认后的映射固化到 refs 目录（外部更换的文件复制进来）
        for spk, path in refs.items():
            target = self._ws.ref_dir / f"ref_{spk}.wav"
            if Path(path).is_file() and Path(path).resolve() != target.resolve():
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, target)
                refs[spk] = str(target)
        self._ws.mark_done("voice")
        return refs

    def _run_tts(self, segments: list, refs: dict, vocals: str) -> None:
        """TTS 逐句生成 + 异常复听（在 TTS 步骤内完成）。

        逐句切片克隆：每句用其自身的人声切片（人工审核/句子切片）做参考，
        继承原句语气/情绪（短句同样直接用自身切片）；仅近无声/切割失败的
        句子回退说话人级参考。配音文件以 开始时间-结束时间-说话人ID-译文
        命名，与源切片一一对应。
        """
        from app.pipeline.tts import (
            STRETCH_REVIEW_HIGH,
            STRETCH_REVIEW_LOW,
            TTSLoadMemoryError,
            TTSEngine,
            tts_runtime_ready,
        )
        from app.pipeline.voice_pick import cut_sentence_slices, sentence_base_name

        cfg = self._cfg
        ok, detail = tts_runtime_ready()
        if not ok:
            raise RuntimeError(tr("runner.tts_env_missing", detail=detail))

        self._ws.tts_dir.mkdir(parents=True, exist_ok=True)
        for old in self._ws.tts_dir.glob("*.wav"):
            try:  # 清空旧配音，文件夹与本次待合成集合一致
                old.unlink()
            except OSError:
                pass
        todo = [
            (i, seg) for i, seg in enumerate(segments)
            if not seg.deleted and str(seg.target_text).strip()
        ]
        progress = self._stage_progress(STAGE_TTS)

        # 切逐句参考（占用 TTS 阶段前 5% 进度，避免与权重加载进度互相回退）
        def _cut_progress(fraction: float, message: str) -> None:
            progress(min(fraction, 1.0) * 0.05, message)

        seg_refs, own_cnt, fb_cnt = cut_sentence_slices(
            segments, vocals, refs, self._ws.seg_slice_dir,
            progress=_cut_progress,
        )
        self._log(tr("runner.seg_ref_ready", own=own_cnt, fallback=fb_cnt))

        def _tts_out(seg) -> Path:
            """配音成品路径（命名：开始时间-结束时间-说话人ID-译文）。"""
            return self._ws.tts_dir / (
                sentence_base_name(seg.start, seg.end, seg.spk, seg.target_text)
                + ".wav"
            )

        def _retire_old(seg, new_out: Path) -> None:
            """移除与新命名不一致的旧配音文件（复听改文后重生成场景）。"""
            old = str(seg.tts_audio_path or "")
            if old and old != str(new_out):
                try:
                    Path(old).unlink()
                except OSError:
                    pass

        engine = TTSEngine()
        device = cfg.device
        device_used = device  # 首轮实际跑通的设备，复听重生成时复用
        try:
            engine.load(cfg.tts_model_dir, device=device, progress=progress)
            done = 0
            for i, seg in todo:
                self._check()
                ref = seg_refs.get(i)
                if not ref:
                    self._log(tr("runner.tts_no_ref", spk=seg.spk))
                    continue
                out = _tts_out(seg)
                raw_dur, ratio = engine.synthesize(
                    seg.target_text, ref, seg.duration, str(out),
                    lang=cfg.target_lang_code,
                )
                _retire_old(seg, out)
                seg.tts_audio_path = str(out)
                seg.needs_review = not (STRETCH_REVIEW_LOW <= ratio <= STRETCH_REVIEW_HIGH)
                done += 1
                self._stage_progress(STAGE_TTS)(
                    done / len(todo),
                    tr("runner.tts_progress", done=done, total=len(todo)),
                )
        except TTSLoadMemoryError:
            # 内存/页面文件不足：换 CPU 重载只会同时驻留更多内存，直接失败
            engine.unload()
            raise
        except _PipelineCancelled:
            # 加载中用户取消：不触发 CPU 回退重载，直接终止
            engine.unload()
            raise
        except Exception as exc:
            engine.unload()
            if device == "cpu":
                raise
            self._log(tr("runner.device_fallback", stage=tr("step.tts"), error=exc))
            device_used = "cpu"
            engine = TTSEngine()
            engine.load(cfg.tts_model_dir, device="cpu", progress=progress)
            done = 0
            for i, seg in todo:
                self._check()
                if seg.tts_audio_path and Path(seg.tts_audio_path).is_file():
                    done += 1
                    continue  # 已在失败前生成
                ref = seg_refs.get(i)
                if not ref:
                    continue
                out = _tts_out(seg)
                raw_dur, ratio = engine.synthesize(
                    seg.target_text, ref, seg.duration, str(out),
                    lang=cfg.target_lang_code,
                )
                _retire_old(seg, out)
                seg.tts_audio_path = str(out)
                seg.needs_review = not (STRETCH_REVIEW_LOW <= ratio <= STRETCH_REVIEW_HIGH)
                done += 1
                self._stage_progress(STAGE_TTS)(
                    done / len(todo),
                    tr("runner.tts_progress", done=done, total=len(todo)),
                )
        finally:
            engine.unload()

        self._ws.save_segments(self._ws.tts_json, segments)

        # 异常复听（存在伸缩超限句时暂停；对话框就地修改译文）
        review_items = [
            (i, seg) for i, seg in enumerate(segments) if seg.needs_review
        ]
        if review_items:
            self._log(tr("runner.tts_review_needed", count=len(review_items)))
            result = self._request_review(ReviewPayload(
                kind="tts",
                segments=[seg for _i, seg in review_items],
                ref_map={"indices": [i for i, _seg in review_items]},
            ))
            # 勾选重生成的句子用（可能已修改的）译文重新合成
            regen_local = list(result.regenerate_indices or [])
            if regen_local:
                engine = TTSEngine()
                try:
                    engine.load(
                        cfg.tts_model_dir,
                        device=device_used, progress=progress,
                    )
                    for done, local_idx in enumerate(regen_local, 1):
                        self._check()
                        gi, seg = review_items[local_idx]
                        ref = seg_refs.get(gi)
                        if not ref:
                            continue
                        out = _tts_out(seg)
                        _raw, _ratio = engine.synthesize(
                            seg.target_text, ref, seg.duration, str(out),
                            lang=cfg.target_lang_code,
                        )
                        _retire_old(seg, out)
                        seg.tts_audio_path = str(out)
                        progress(
                            done / len(regen_local),
                            tr("runner.tts_regen_progress", done=done, total=len(regen_local)),
                        )
                finally:
                    engine.unload()
            # 复听确认后清除全部待复核标记并保存
            for _i, seg in review_items:
                seg.needs_review = False
            self._ws.save_segments(self._ws.tts_json, segments)

        self._ws.mark_done(STAGE_TTS)
        self._stage_end(STAGE_TTS)

    # ------------------------------------------------------------------ LLM

    def _llm_session(self):
        """按需启动 llama-server 的上下文管理器（翻译/校对共用）。"""
        import contextlib

        from app.pipeline.llm_engine import LLMClient, LlamaServerManager

        cfg = self._cfg
        runner = self

        @contextlib.contextmanager
        def _session():
            server = LlamaServerManager(cfg.llm_server_exe)
            server.start(
                cfg.llm_model_path, device=cfg.device,
                progress=runner._log_progress(),
            )
            try:
                yield LLMClient(server.base_url)
            finally:
                server.stop()

        return _session()

    # ------------------------------------------------------------------ 暂停/取消

    def _request_review(self, payload: ReviewPayload) -> ReviewResult:
        """请求人工审核；任务勾选"自动通过"时直接采纳当前结果不暂停。"""
        if self._cfg.auto_accept_reviews:
            self._log(tr("runner.auto_review_passed"))
            if payload.kind == "voice":
                return ReviewResult(accepted=True, ref_map=payload.ref_map)
            if payload.kind == "tts":
                return ReviewResult(accepted=True, regenerate_indices=[])
            return ReviewResult(accepted=True)
        return self._pause_for_review(payload)

    @staticmethod
    def _read_text(path: Path) -> str:
        """读取文本文件内容；文件不存在或不可读返回空串。"""
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    def _pause_for_review(self, payload: ReviewPayload) -> ReviewResult:
        """发射审核请求并阻塞等待 UI 回传；取消时抛 _PipelineCancelled。"""
        self._review_event.clear()
        self._review_result = None
        self.review_requested.emit(payload)
        self._review_event.wait()
        self._check()  # cancel() 唤醒时在此抛出
        result = self._review_result
        if result is None or not result.accepted:
            raise _PipelineCancelled()
        return result

    def _check(self) -> None:
        """取消检查点。"""
        if self._cancel.is_set():
            raise _PipelineCancelled()

    # ------------------------------------------------------------------ 进度/日志

    def _build_weights(self, dub: bool, is_video: bool, translate_needed: bool) -> None:
        weights: list[tuple[str, float]] = [
            (STAGE_EXTRACT, 3.0),
        ]
        if dub:
            weights.append((STAGE_SEPARATE, 12.0))
        weights += [
            (STAGE_ASR, 25.0),
            (STAGE_PROOFREAD, 8.0),
            (STAGE_REVIEW, 1.0),
        ]
        if translate_needed:
            weights += [(STAGE_TRANSLATE, 8.0), (STAGE_REVIEW_TRANSLATION, 1.0)]
        if dub:
            weights += [
                (STAGE_TTS, 30.0),
                (STAGE_MIX, 6.0),
            ]
            if is_video:
                weights.append((STAGE_MUX, 4.0))
        self._weights = weights
        self._total_weight = sum(w for _s, w in weights) or 1.0
        self._progress_base = 0.0

    def _weight_span(self, stage: str) -> tuple[float, float]:
        """返回阶段在总进度中的 [起点, 终点)（0-1）。"""
        base = 0.0
        for name, weight in self._weights:
            if name == stage:
                return base, base + weight
            base += weight
        return base, base

    def _stage_begin(self, stage: str) -> None:
        self._last_stage = stage
        self._last_log_message = ""
        self.step_state_changed.emit(stage, "active")

    def _stage_end(self, stage: str) -> None:
        self._progress_base = self._weight_span(stage)[1]
        self.step_state_changed.emit(stage, "done")
        self.progress_changed.emit(int(self._progress_base / self._total_weight * 100))

    def _skip_stage(self, stage: str) -> None:
        """跳过已完成阶段（复用产物）。"""
        self._log(tr("runner.stage_skipped", stage=tr(f"step.{stage}")))
        self._stage_end(stage)

    def _stage_progress(self, stage: str) -> ProgressCallback:
        """构造阶段进度回调：映射到总进度、去重日志、承担取消检查点。"""
        runner = self
        start, end = self._weight_span(stage)

        def _callback(fraction: float, message: str) -> None:
            runner._check()
            runner.progress_changed.emit(
                int((start + (end - start) * min(max(fraction, 0.0), 1.0))
                    / runner._total_weight * 100)
            )
            if message and message != runner._last_log_message:
                runner._last_log_message = message
                runner.log_emitted.emit(message)

        return _callback

    def _null_progress(self) -> ProgressCallback:
        """仅承担取消检查的空进度回调（llama-server 启动等场景）。"""
        runner = self

        def _callback(_fraction: float, _message: str) -> None:
            runner._check()

        return _callback

    def _log_progress(self) -> ProgressCallback:
        """转发日志消息、不影响进度条的回调（llama-server 启动可见 GPU/CPU 状态）。"""
        runner = self

        def _callback(_fraction: float, message: str) -> None:
            runner._check()
            if message:
                runner.log_emitted.emit(message)

        return _callback

    def _log(self, message: str) -> None:
        self.log_emitted.emit(message)
