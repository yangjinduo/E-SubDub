# -*- coding: utf-8 -*-
"""流程编排引擎：QThread 中按阶段串行执行流水线，支持人工审核暂停点。

阶段顺序（配音模式全量；仅字幕模式跳过 人声分离 / TTS / 配音复核 / 混音，
封装输出退化为归档字幕副本）：
音频提取 → 人声分离 → ASR → 文本审核（人工）→
LLM 翻译（Index-Translate-2B，配音任务追加 Index-Homura-2B 时长修正）→
[译文人工审核] → SRT 字幕 → 参考音色挑选/[音色管理] →
TTS → [配音复核] → 混音 → MKV 封装。

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
    STAGE_TTS_REVIEW,
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


def _tts_out_path(ws, seg) -> Path:
    """配音成品路径（命名：开始时间-结束时间-说话人ID-译文）。"""
    from app.pipeline.voice_pick import sentence_base_name

    return ws.tts_dir / (
        sentence_base_name(seg.start, seg.end, seg.spk, seg.target_text) + ".wav"
    )


def _retire_old_audio(seg, new_out: Path) -> None:
    """移除与新命名不一致的旧配音文件（复核改文后重生成场景）。"""
    old = str(seg.tts_audio_path or "")
    if old and old != str(new_out):
        try:
            Path(old).unlink()
        except OSError:
            pass


def detect_language_code(segments: list) -> str:
    """按字符集启发式检测分段文本的语言代码。

    源语言选择"自动检测"时用于翻译提示词与界面显示：优先级为
    假名（日语）> 谚文（韩语）> 汉字（中文）> 西里尔（俄语），
    拉丁字母文本无法细分英/法/德/西，按英语处理。
    """
    sample = "".join(s.text for s in segments if not s.deleted)[:4000]
    has_kana = has_hangul = has_han = has_cyrillic = has_latin = False
    for ch in sample:
        cp = ord(ch)
        if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
            has_kana = True
        elif 0xAC00 <= cp <= 0xD7A3 or 0x1100 <= cp <= 0x11FF:
            has_hangul = True
        elif 0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF:
            has_han = True
        elif 0x0400 <= cp <= 0x04FF:
            has_cyrillic = True
        elif 0x0041 <= cp <= 0x007A or 0x00C0 <= cp <= 0x024F:
            has_latin = True
    if has_kana:
        return "ja"
    if has_hangul:
        return "ko"
    if has_han:
        return "zh"
    if has_cyrillic:
        return "ru"
    return "en"  # 拉丁字母（英/法/德/西不可细分）与无法识别时按英语


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
    overlay_original_audio: bool = True  # 混音后将配音对白按时间线覆盖回原声（保留句间语气词，默认开启）
    asr_model_dir: str = ""
    tts_model_dir: str = ""
    llm_model_path: str = ""       # 翻译模型 GGUF 路径（Index-Translate-2B）
    homura_model_path: str = ""    # 时长修正模型 GGUF 路径（Index-Homura-2B，可选）
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
    sample_map: dict = field(default_factory=dict)  # voice：{spk: 参考句文本}
    source_lang: str = ""
    target_lang: str = ""
    regen_hook: object = None  # tts：即时重生成回调（局部行索引, progress=None → 伸缩比）


@dataclass
class ReviewResult:
    """审核结果（UI 线程通过 submit_review 回传）。"""

    accepted: bool
    ref_map: dict = field(default_factory=dict)      # voice：确认后的映射
    regenerate_indices: list = field(default_factory=list)  # tts：需重生成的索引


@dataclass
class RunResult:
    """全部完成后的产物信息（finished_ok 信号负载）。"""

    srt_path: str              # 成品字幕（10_封装输出 夹内副本）
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
        # 当前 llama-server 进程管理器（cancel() 需从 UI 线程杀掉进程让
        # 阻塞中的 LLM 请求立刻失败；None = 未处于 LLM 阶段）
        self._llm_server = None
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
        """请求取消（在下一个检查点生效）。

        LLM 阶段额外立刻杀死 llama-server：阻塞中的 HTTP 请求（单批次
        CPU 推理可达数分钟）随之快速失败，取消不必等当前批次生成完；
        由此引发的异常由 run() 统一按取消收场。
        """
        self._cancel.set()
        self._review_event.set()  # 唤醒可能正在等待的审核暂停
        server = self._llm_server
        if server is not None:
            server.kill()

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
            if self._cancel.is_set():
                # 取消窗口内终止外部资源（如杀掉 llama-server）引发的异常
                # 按"已取消"而非"失败"收场
                self.cancelled.emit()
            else:
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

        # 4. 文本审核（人工）：ASR 结果进入人工审核窗口定稿，
        #    审核定稿存 04_文本审核/reviewed.json（含逐句切片目录）
        self._stage_begin(STAGE_PROOFREAD)
        if self._ws.is_done(STAGE_REVIEW):
            segments = self._ws.load_segments(self._ws.reviewed_json)
            self._skip_stage(STAGE_PROOFREAD)
        else:
            pre_state = self._segments_fingerprint(segments)
            self._request_review(ReviewPayload(
                kind="asr", segments=segments, audio_wav=vocals,
            ))
            self._ws.save_segments(self._ws.reviewed_json, segments)
            if self._segments_fingerprint(segments) != pre_state:
                # 人工修改过内容：下游翻译及其后全部失效重跑
                self._ws.reset_stages([
                    STAGE_TRANSLATE, STAGE_REVIEW_TRANSLATION, STAGE_TTS,
                    STAGE_TTS_REVIEW, STAGE_MIX, STAGE_MUX,
                ])
            self._ws.mark_done(STAGE_REVIEW)
            self._stage_end(STAGE_PROOFREAD)
        self._check()

        # 源语言"自动检测"：从定稿文本推断实际语言（翻译提示词与审核窗口显示用），
        # 并回写到任务配置——后续 SRT 文件名等以检测出的语言代号为准
        source_code = cfg.source_lang_code
        if source_code == "auto":
            source_code = detect_language_code(segments)
            cfg.source_lang_code = source_code
            self._log(tr("runner.lang_detected",
                         lang=LANG_NAMES.get(source_code, source_code)))
        source_name = LANG_NAMES.get(source_code, "原文语言")

        # 5. LLM 翻译（源=目标语言时直接复用原文）；配音任务随后用
        #    Index-Homura-2B 做时长修正二遍（仅中文源 → en/ja/ar/es）
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
                    source_name,
                    LANG_NAMES.get(cfg.target_lang_code, "目标语言"),
                    progress=self._partial_progress(STAGE_TRANSLATE, 0.0, 0.9),
                )
            if self._homura_needed(source_code):
                with self._llm_session(cfg.homura_model_path) as client:
                    from app.pipeline.translate import homura_length_fix
                    segments, fixed, overlong = homura_length_fix(
                        client, segments,
                        cfg.target_lang_code,
                        LANG_NAMES.get(cfg.target_lang_code, "目标语言"),
                        progress=self._partial_progress(STAGE_TRANSLATE, 0.9, 1.0),
                    )
                if overlong:
                    self._log(tr("runner.homura_fix", fixed=fixed, overlong=overlong))
            self._ws.save_segments(self._ws.translated_pre, segments)
            self._ws.mark_done(STAGE_TRANSLATE)
            self._stage_end(STAGE_TRANSLATE)
        self._check()

        # 6. 译文人工审核
        self._stage_begin(STAGE_REVIEW_TRANSLATION)
        if self._ws.is_done(STAGE_REVIEW_TRANSLATION):
            segments = self._ws.load_segments(self._ws.translated_post)
            self._skip_stage(STAGE_REVIEW_TRANSLATION)
        else:
            pre_text = self._read_text(self._ws.translated_pre)
            self._request_review(ReviewPayload(
                kind="translation", segments=segments, audio_wav=vocals,
                source_lang=source_name,
                target_lang=LANG_NAMES.get(cfg.target_lang_code, ""),
            ))
            self._ws.save_segments(self._ws.translated_post, segments)
            if self._read_text(self._ws.translated_post) != pre_text:
                self._ws.reset_stages([
                    STAGE_TTS, STAGE_TTS_REVIEW, STAGE_MIX, STAGE_MUX,
                ])
            self._ws.mark_done(STAGE_REVIEW_TRANSLATION)
            self._stage_end(STAGE_REVIEW_TRANSLATION)
        self._check()

        # 7. SRT 字幕（文件名随字幕形式：单语仅目标语种代号，双语上位语言在前；
        #    源语言"自动检测"时以检测出的代号命名）
        srt_mode = SUBTITLE_MODE_MAP.get(cfg.subtitle_mode, MODE_MONO)
        srt_path = self._ws.srt_path(
            cfg.target_lang_code, source_code,
            source_on_top=(srt_mode == MODE_BILINGUAL_SOURCE_TOP),
        )
        generate_srt(
            segments, str(srt_path), srt_mode,
            source_lang=source_code, target_lang=cfg.target_lang_code,
        )
        self._log(tr("runner.srt_done", path=srt_path.name, count=len(segments)))
        # SRT 生成时的译文快照（配音复核中修改译文后对比同步用）
        srt_texts = [seg.target_text for seg in segments]

        # 8+. 配音链路
        audio_out = ""
        mkv_out = ""
        # 封装输出夹：成品归档目的地（MKV / 配音音频副本 / 字幕副本）
        mux_dir = self._ws.ensure_stage_dir(STAGE_MUX)
        if dub:
            # 8. 参考音色挑选 + 音色管理审核
            refs = self._pick_and_review_voices(segments, vocals)

            # 9. TTS 逐句生成（引擎保持加载，供配音复核即时重生成复用，
            #    复核阶段结束后统一释放）
            tts_ctx: dict = {}
            self._stage_begin(STAGE_TTS)
            if self._ws.is_done(STAGE_TTS):
                segments = self._ws.load_segments(self._ws.tts_json)
                self._skip_stage(STAGE_TTS)
            else:
                tts_ctx = self._run_tts(segments, refs, vocals)
            self._check()

            # 10. 配音复核（TTS 与混音之间的独立暂停点：试听、修改译文、
            #     即时重生成；复核产物存 tts_reviewed.json）
            self._stage_begin(STAGE_TTS_REVIEW)
            if self._ws.is_done(STAGE_TTS_REVIEW):
                segments = self._ws.load_segments(self._ws.tts_reviewed_json)
                self._skip_stage(STAGE_TTS_REVIEW)
            else:
                # 复核标记被重置后重入（如手动改 manifest 再进复核窗口）：
                # 优先载入上次复核结果，保留此前改过的译文
                if self._ws.tts_reviewed_json.is_file():
                    segments = self._ws.load_segments(self._ws.tts_reviewed_json)
                self._review_tts(segments, tts_ctx)
            self._check()
            engine = tts_ctx.get("engine")
            if engine is not None:
                engine.unload()
                tts_ctx["engine"] = None

            # 复核中换过译文的句子同步回 SRT：与快照做字符串对比
            # （纯内存比较，算力开销可忽略），有差异时整体重写一次字幕文件
            edited = sum(
                1 for old, seg in zip(srt_texts, segments)
                if old != seg.target_text
            )
            if edited:
                generate_srt(
                    segments, str(srt_path), srt_mode,
                    source_lang=source_code, target_lang=cfg.target_lang_code,
                )
                self._log(tr("runner.srt_synced", count=edited))

            # 11. 混音
            self._stage_begin(STAGE_MIX)
            # 叠加原声开关与上次混音时不同：成品形态改变，混音/封装失效重跑
            # （未记录开关的旧任务按"未叠加"处理，勾选后重跑即生效）
            if (
                self._ws.is_done(STAGE_MIX)
                and bool(self._ws.stage_flag("mix_overlay_original"))
                != cfg.overlay_original_audio
            ):
                self._ws.reset_stages([STAGE_MIX, STAGE_MUX])
            if self._ws.is_done(STAGE_MIX):
                self._skip_stage(STAGE_MIX)
            else:
                from app.pipeline.mix import mix_tracks
                self._ws.ensure_stage_dir(STAGE_MIX)
                if cfg.overlay_original_audio:
                    self._log(tr("runner.mix_overlay"))
                mix_tracks(
                    segments, no_vocals, str(self._ws.mixed_audio), vocals,
                    progress=self._stage_progress(STAGE_MIX),
                    full_audio_wav=full_audio if cfg.overlay_original_audio else "",
                )
                self._ws.set_stage_flag("mix_overlay_original", cfg.overlay_original_audio)
                self._ws.mark_done(STAGE_MIX)
                self._stage_end(STAGE_MIX)
            audio_out = str(self._ws.mixed_audio)
            self._check()

            # 12. MKV 封装（音频输入无视频流可封装，混音成品直落封装夹）
            if is_video:
                self._stage_begin(STAGE_MUX)
                if self._ws.is_done(STAGE_MUX):
                    self._skip_stage(STAGE_MUX)
                else:
                    from app.pipeline.mux import mux_mkv
                    mux_mkv(
                        cfg.media_path, audio_out, str(srt_path),
                        str(self._ws.output_mkv),
                        audio_lang=FFMPEG_LANGS.get(cfg.target_lang_code, "und"),
                        subtitle_lang=FFMPEG_LANGS.get(cfg.target_lang_code, "und"),
                        audio_title=f"AI dubbing {cfg.target_lang_code.upper()}",
                        progress=self._stage_progress(STAGE_MUX),
                    )
                    shutil.copy2(srt_path, mux_dir / srt_path.name) # 封装夹内同时保留处理好的字幕副本
                    self._ws.mark_done(STAGE_MUX)
                    self._stage_end(STAGE_MUX)
                mkv_out = str(self._ws.output_mkv)
            else:
                # 音频输入无视频流可封装：混音成品与字幕副本归档到封装夹
                shutil.copy2(self._ws.mixed_audio, self._ws.dubbed_wav)
                shutil.copy2(srt_path, mux_dir / srt_path.name)
                audio_out = str(self._ws.dubbed_wav)
                self._log(tr("runner.mux_skipped_audio_input"))
        else:
            # 仅字幕任务（无配音链路）：成品 SRT 归档到封装输出夹
            self._stage_begin(STAGE_MUX)
            if self._ws.is_done(STAGE_MUX):
                self._skip_stage(STAGE_MUX)
            else:
                shutil.copy2(srt_path, mux_dir / srt_path.name)
                self._ws.mark_done(STAGE_MUX)
                self._stage_end(STAGE_MUX)

        self.progress_changed.emit(100)
        return RunResult(
            # 成品 SRT 一律指封装输出夹内的副本（根目录母本为中间产物）
            srt_path=str(mux_dir / srt_path.name), mkv_path=mkv_out,
            audio_path=audio_out, workspace=self._ws,
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
            # 用户取消不按设备故障处理（token 级进度回调内即触发取消检查点）
            if device == "cpu" or isinstance(exc, _PipelineCancelled):
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

        refs, samples = pick_reference_segments(
            segments, vocals, self._ws.ref_dir,
            progress=self._null_progress(),
        )
        if not refs:
            raise RuntimeError(tr("runner.voice_none"))
        self._log(tr("runner.voice_picked", count=len(refs)))

        result = self._request_review(ReviewPayload(
            kind="voice", segments=segments, audio_wav=vocals, ref_map=refs,
            sample_map=samples,
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

    def _run_tts(self, segments: list, refs: dict, vocals: str) -> dict:
        """TTS 逐句生成（不含复核），返回复核阶段复用的上下文。

        逐句切片克隆：每句用其自身的人声切片（人工审核/句子切片）做参考，
        继承原句语气/情绪（短句同样直接用自身切片）；仅近无声/切割失败的
        句子回退说话人级参考。配音文件以 开始时间-结束时间-说话人ID-译文
        命名，与源切片一一对应。

        返回 {"engine": 已加载的 TTSEngine, "seg_refs": {索引: 参考音频},
        "todo": [(索引, 分段), …]}；合成完成后引擎保持加载（配音复核的
        即时重生成复用，避免重复加载 10GB 级模型），由复核阶段结束后释放。
        异常/取消时在本地释放后再上抛。
        """
        from app.pipeline.tts import (
            STRETCH_REVIEW_HIGH,
            STRETCH_REVIEW_LOW,
            TTSLoadMemoryError,
            TTSEngine,
            tts_runtime_ready,
        )
        from app.pipeline.voice_pick import cut_sentence_slices

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

        # 阶段内进度布局：逐句切片占前 5%，模型加载占 5%-10%，
        # 逐句合成占后 90% —— 三段单调衔接，进度条不再回退
        def _cut_progress(fraction: float, message: str) -> None:
            progress(min(fraction, 1.0) * 0.05, message)

        def _load_progress(fraction: float, message: str) -> None:
            progress(0.05 + min(fraction, 1.0) * 0.05, message)

        def _synth_progress(fraction: float, message: str) -> None:
            progress(0.10 + min(fraction, 1.0) * 0.90, message)

        seg_refs, own_cnt, fb_cnt = cut_sentence_slices(
            segments, vocals, refs, self._ws.seg_slice_dir,
            progress=_cut_progress,
        )
        self._log(tr("runner.seg_ref_ready", own=own_cnt, fallback=fb_cnt))

        def _synth(i: int, seg: Segment, engine, idx: int, total: int) -> None:
            """合成单句并记录路径/伸缩比/待复核标记；句内进度按 10%
            档位打日志，进度条按句粒度推进（与 TTS 任务一致）。"""
            ref = seg_refs.get(i)
            if not ref:
                self._log(tr("runner.tts_no_ref", spk=seg.spk))
                return
            out = _tts_out_path(self._ws, seg)

            def _msg(frac: float) -> str:
                pct = int(frac * 100) // 10 * 10
                if pct <= 0:
                    return tr("runner.tts_progress", done=idx + 1, total=total)
                return tr("runner.tts_progress_pct",
                          done=idx + 1, total=total, pct=pct)

            _raw_dur, ratio = engine.synthesize(
                seg.target_text, ref, seg.duration, str(out),
                lang=cfg.target_lang_code,
                progress=lambda frac, _desc="": _synth_progress(
                    idx / total,
                    _msg(frac),
                ),
            )
            _retire_old_audio(seg, out)
            seg.tts_audio_path = str(out)
            seg.stretch_ratio = ratio
            seg.needs_review = not (STRETCH_REVIEW_LOW <= ratio <= STRETCH_REVIEW_HIGH)

        engine = TTSEngine()
        try:
            try:
                engine.load(cfg.tts_model_dir, device=cfg.device,
                            progress=_load_progress)
                total = len(todo)
                done = 0
                for pos, (i, seg) in enumerate(todo):
                    self._check()
                    # 句开始即打日志（确认任务已开工）；句内 10% 档位消息
                    # 经 synthesize 回调逐级显示，0% 档与句开始消息相同
                    # 被去重机制吞掉
                    _synth_progress(
                        pos / total,
                        tr("runner.tts_progress", done=pos + 1, total=total),
                    )
                    _synth(i, seg, engine, pos, total)
                    done += 1
                    # 句完成补 100% 档（与句内 90% 档衔接成完整序列）
                    _synth_progress(
                        done / total,
                        tr("runner.tts_progress_pct",
                           done=done, total=total, pct=100),
                    )
            except Exception as exc:
                # GPU 失败换 CPU 重试；内存不足与用户取消不重试（直接上抛）
                if cfg.device == "cpu" or isinstance(
                    exc, (TTSLoadMemoryError, _PipelineCancelled)
                ):
                    raise
                self._log(tr("runner.device_fallback", stage=tr("step.tts"), error=exc))
                engine.unload()
                engine.load(cfg.tts_model_dir, device="cpu",
                            progress=_load_progress)
                done = 0
                for i, seg in todo:
                    self._check()
                    if seg.tts_audio_path and Path(seg.tts_audio_path).is_file():
                        done += 1
                        continue  # 已在失败前生成
                    _synth_progress(
                        done / total,
                        tr("runner.tts_progress", done=done + 1, total=total),
                    )
                    _synth(i, seg, engine, done, total)
                    done += 1
                    # 句完成补 100% 档（与句内 90% 档衔接成完整序列）
                    _synth_progress(
                        done / total,
                        tr("runner.tts_progress_pct",
                           done=done, total=total, pct=100),
                    )

            self._ws.save_segments(self._ws.tts_json, segments)
            self._ws.mark_done(STAGE_TTS)
            self._stage_end(STAGE_TTS)
            return {"engine": engine, "seg_refs": seg_refs, "todo": todo}
        except BaseException:
            engine.unload()  # 合成中断：引擎无人接手释放，就地卸载
            raise

    def _review_tts(self, segments: list, tts_ctx: dict) -> None:
        """配音复核（独立暂停点）：试听、修改译文、即时重生成。

        全部已合成句子进入对话框；伸缩比异常句以红色粗体标出。复核
        未决期间引擎保持加载：对话框经 regen_hook 复用引擎逐句重生成
        （runner 线程此时阻塞在等待审核结果，不会并发使用引擎）。
        TTS 阶段被跳过（复用产物）时，引擎按需加载、参考映射按切片
        文件名从磁盘重建。复核结果存 tts_reviewed.json。
        """
        from app.pipeline.tts import (
            STRETCH_REVIEW_HIGH,
            STRETCH_REVIEW_LOW,
            TTSEngine,
        )
        from app.pipeline.voice_pick import sentence_base_name

        cfg = self._cfg
        todo = tts_ctx.get("todo")
        if todo is None:
            todo = [
                (i, seg) for i, seg in enumerate(segments)
                if not seg.deleted and str(seg.target_text).strip()
            ]
        seg_refs = dict(tts_ctx.get("seg_refs") or {})

        # 参考映射缺失（TTS 复用跳过）：逐句切片优先，缺失回退说话人参考
        for i, seg in todo:
            if i in seg_refs:
                continue
            own = self._ws.seg_slice_dir / (
                sentence_base_name(seg.start, seg.end, seg.spk, seg.text) + ".wav"
            )
            if own.is_file():
                seg_refs[i] = str(own)
                continue
            spk_ref = self._ws.ref_dir / f"ref_{seg.spk}.wav"
            if spk_ref.is_file():
                seg_refs[i] = str(spk_ref)

        engine = tts_ctx.get("engine")
        if engine is None and todo:
            # 复核可能即时重生成：预加载引擎（复核期间保持加载）
            from app.pipeline.tts import tts_runtime_ready

            ok, detail = tts_runtime_ready()
            if not ok:
                raise RuntimeError(tr("runner.tts_env_missing", detail=detail))
            engine = TTSEngine()
            try:
                engine.load(
                    cfg.tts_model_dir, device=cfg.device,
                    progress=self._log_progress(),
                )
            except Exception as exc:
                if cfg.device == "cpu" or isinstance(exc, _PipelineCancelled):
                    raise
                self._log(tr("runner.device_fallback", stage=tr("step.tts"), error=exc))
                engine.load(
                    cfg.tts_model_dir, device="cpu",
                    progress=self._log_progress(),
                )
            tts_ctx["engine"] = engine

        if todo:
            self._log(tr("runner.tts_review_needed", count=len(todo)))

            def _regen_one(local_idx: int, progress=None) -> float:
                """对话框内即时重生成一句（对话框后台线程调用，
                progress 为单句进度回调，转给 TTS 引擎锚点+插值上报）。"""
                gi, seg = todo[local_idx]
                ref = seg_refs.get(gi)
                if not ref:
                    raise RuntimeError(tr("runner.tts_no_ref", spk=seg.spk))
                out = _tts_out_path(self._ws, seg)
                _raw, ratio = engine.synthesize(
                    seg.target_text, ref, seg.duration, str(out),
                    lang=cfg.target_lang_code, progress=progress,
                )
                _retire_old_audio(seg, out)
                seg.tts_audio_path = str(out)
                seg.stretch_ratio = ratio
                seg.needs_review = not (STRETCH_REVIEW_LOW <= ratio <= STRETCH_REVIEW_HIGH)
                return ratio

            # 复核可能改译文/换配音文件：有变化时下游混音/封装失效重跑
            pre_state = [
                (str(seg.tts_audio_path), seg.target_text) for seg in segments
            ]
            self._request_review(ReviewPayload(
                kind="tts",
                segments=[seg for _i, seg in todo],
                ref_map={"indices": [i for i, _seg in todo]},
                regen_hook=_regen_one,
            ))
            # 重生成已在对话框内即时完成；确认后清除待复核标记并保存
            for _i, seg in todo:
                seg.needs_review = False
            if [
                (str(seg.tts_audio_path), seg.target_text) for seg in segments
            ] != pre_state:
                self._ws.reset_stages([STAGE_MIX, STAGE_MUX])

        self._ws.save_segments(self._ws.tts_reviewed_json, segments)
        self._ws.mark_done(STAGE_TTS_REVIEW)
        self._stage_end(STAGE_TTS_REVIEW)

    def _homura_needed(self, source_code: str) -> bool:
        """是否执行 Homura 时长修正二遍（配音 + 中文源 + 支持的目标语言 + 模型就绪）。"""
        from app.pipeline.translate import HOMURA_TARGETS

        cfg = self._cfg
        return bool(
            cfg.dubbing_enabled
            and cfg.homura_model_path
            and source_code == "zh"
            and cfg.target_lang_code in HOMURA_TARGETS
        )

    # ------------------------------------------------------------------ LLM

    def _llm_session(self, model_path: str = ""):
        """按需启动 llama-server 的上下文管理器。

        model_path 为空时用主翻译模型（Index-Translate-2B）；
        时长修正二遍传 Homura GGUF 路径（顺序启停，不双开实例）。
        """
        import contextlib

        from app.pipeline.llm_engine import LLMClient, LlamaServerManager

        cfg = self._cfg
        runner = self

        @contextlib.contextmanager
        def _session():
            server = LlamaServerManager(cfg.llm_server_exe)
            runner._llm_server = server  # 供 cancel() 立即终止进程
            server.start(
                model_path or cfg.llm_model_path, device=cfg.device,
                progress=runner._log_progress(),
            )
            try:
                yield LLMClient(server.base_url)
            finally:
                runner._llm_server = None
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

    @staticmethod
    def _segments_fingerprint(segments: list) -> list:
        """分段内容指纹（人工审核修改检测：影响下游翻译/TTS 的字段）。"""
        return [
            (s.spk, round(float(s.start), 3), round(float(s.end), 3),
             s.text, bool(s.deleted))
            for s in segments
        ]

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
            (STAGE_PROOFREAD, 9.0),  # 人工审核（文本审核步骤）
        ]
        if translate_needed:
            # 翻译含 Homura 时长修正二遍（主翻译 90% + 修正 10% 区间）
            weights += [(STAGE_TRANSLATE, 8.0), (STAGE_REVIEW_TRANSLATION, 1.0)]
        if dub:
            weights += [
                (STAGE_TTS, 30.0),
                (STAGE_TTS_REVIEW, 1.0),
                (STAGE_MIX, 6.0),
            ]
        # 封装输出：视频配音任务封装 MKV（4.0）；纯字幕任务仅归档 SRT
        # 副本（1.0）；音频输入任务无独立封装阶段（成品直落封装夹）
        if dub and is_video:
            weights.append((STAGE_MUX, 4.0))
        elif not dub:
            weights.append((STAGE_MUX, 1.0))
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
        return self._partial_progress(stage, 0.0, 1.0)

    def _partial_progress(
        self, stage: str, begin_frac: float, end_frac: float,
    ) -> ProgressCallback:
        """构造阶段内局部进度回调：fraction 映射到阶段进度区间的
        [begin_frac, end_frac) 段（如翻译步骤中主翻译占前 90%、
        Homura 时长修正占后 10%）。"""
        runner = self
        start, end = self._weight_span(stage)
        lo = start + (end - start) * begin_frac
        hi = start + (end - start) * end_frac

        def _callback(fraction: float, message: str) -> None:
            runner._check()
            runner.progress_changed.emit(
                int((lo + (hi - lo) * min(max(fraction, 0.0), 1.0))
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
