# -*- coding: utf-8 -*-
"""文本转语音任务：纯 TTS 流水线（无 ASR/翻译/混音链路）。

阶段（与 pipeline_panel 的 TTS_PIPELINE_STEPS 对应）：
01 文本处理 → 分句（。！？!?… 换行，英文句点仅后随空白时切分）、
    语言自动检测（langid + 中英混排判 zhen）、参考音频 ffmpeg 规范化
    （22050Hz 单声道 WAV，规避解码兼容问题）；
02 语音合成 → IndexTTS-2.5 逐句克隆合成（自由时长：不向原片时长对齐，
    只裁首尾静音），句级断点续跑（synthesis.json 已有且 WAV 存在的句子
    直接复用）；情绪按句定制（emo_vector），语速经 duration_factor=1/语速；
03 语音复核 → 独立人工暂停点：试听逐句结果，可删除句子（不进成品，
    WAV 保留可恢复）与即时重生成（复用已加载的 TTS 引擎）；结果存
    03_语音复核/reviewed.json；
04 封装输出 → 拼接复核保留句与句间静音（200ms）→ ffmpeg 转
    WAV/MP3/M4A/FLAC → 写 SRT 字幕（时间轴 = 逐句实际时长累计，绝不漂移）。

成品在 04_封装输出 夹：{任务名}.{格式} 与 {任务名}.{语种大写}.srt。
设置快照（manifest 标记）：文本/语速/情绪/参考音频/模型任一变化，或
实际合成语言变化 → 四阶段全部重跑；仅输出格式 / SRT 开关变化 →
只重跑封装输出。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from app.core.i18n import tr
from app.core.runner import ReviewPayload, ReviewResult
from app.core.segment import Segment
from app.core.workspace import (
    STAGE_TTS_MUX,
    STAGE_TTS_SYNTH,
    STAGE_TTS_TEXT,
    STAGE_TTS_VOICE_REVIEW,
    TTS_TASK_STAGE_ORDER,
    Workspace,
)
from app.pipeline.base import ProgressCallback
from app.pipeline.subtitle import MODE_MONO, generate_srt

logger = logging.getLogger(__name__)

# IndexTTS 情绪向量维度顺序（与 infer_v2_5 的 emo_vector 一致）
EMOTION_KEYS: list[str] = [
    "happy", "angry", "sad", "afraid",
    "disgusted", "melancholic", "surprised", "calm",
]

# 输出格式 → ffmpeg 音频编码参数
_FORMAT_CODECS: dict[str, list[str]] = {
    "mp3": ["-codec:a", "libmp3lame", "-q:a", "2"],
    "m4a": ["-codec:a", "aac", "-b:a", "192k"],
    "flac": ["-codec:a", "flac"],
}

# 句间静音（秒）：拼接时的固定间隔，对齐 IndexTTS infer 内部 interval_silence
INTERVAL_SILENCE_SEC = 0.2

# TTS 输出采样率（IndexTTS-2.5 固定 22050Hz PCM16）
TTS_SAMPLE_RATE = 22050

# TTS 流程布局版本：2 = 四阶段（新增 03_语音复核，封装输出 03→04）；
# 旧版（三阶段）工作夹在 _execute 开头全量重跑（产物目录已变）
TTS_FLOW_VERSION = 2


def emotion_vector(key: str | None) -> list[float] | None:
    """情绪 key → 8 维 one-hot 权重向量；None/未知 key 返回 None（跟随参考音频）。"""
    if not key or key not in EMOTION_KEYS:
        return None
    vector = [0.0] * len(EMOTION_KEYS)
    vector[EMOTION_KEYS.index(key)] = 1.0
    return vector


def normalize_ref_audio(src: Path, out: Path) -> None:
    """参考音频规范化为 22050Hz 单声道 PCM16 WAV（ffmpeg 优先，回退 librosa）。

    runner 的文本处理阶段与流程查看窗口的"更换参考音频"共用。
    """
    out.parent.mkdir(parents=True, exist_ok=True)

    from app.core.ffmpeg import get_ffmpeg_path

    ffmpeg = get_ffmpeg_path()
    if ffmpeg:
        cmd = [
            ffmpeg, "-y", "-i", str(src),
            "-ac", "1", "-ar", str(TTS_SAMPLE_RATE),
            "-sample_fmt", "s16", str(out),
        ]
        proc = subprocess.run(
            cmd, capture_output=True, timeout=120, check=False,
        )
        if proc.returncode == 0 and out.is_file():
            return
        logger.warning("ffmpeg 参考音频规范化失败，回退 librosa: %s",
                      proc.stderr[-300:] if proc.stderr else "")

    # 回退：librosa 解码重采样（ffmpeg 不可用/失败时）
    import librosa
    import soundfile as sf

    wav, _sr = librosa.load(str(src), sr=TTS_SAMPLE_RATE, mono=True)
    sf.write(str(out), wav, TTS_SAMPLE_RATE, subtype="PCM_16")


def split_sentences(text: str) -> list[str]:
    """分句：按 。！？!?… 与换行切分；英文句点仅在后随空白/行尾时切分
    （避免小数、网址误切）；空白与空段丢弃。"""
    if not text:
        return []
    parts = re.split(
        r"(?<=[。！？!?…])\s*|(?<=\.)(?=\s)|\n+",
        text,
    )
    return [p.strip() for p in parts if p.strip()]


@dataclass
class TtsTaskConfig:
    """文本转语音任务配置（任务窗口校验资源后构造）。"""

    task_name: str
    text: str
    ref_audio_path: str
    output_dir: str
    lang_code: str               # "auto"/"zh"/"en"/"ja"/"es"/"ar"/"zhen"
    speed: float                 # 语速 0.5-2.0（1.0 正常）
    generate_srt: bool
    output_format: str           # "wav"/"mp3"/"m4a"/"flac"
    device: str                  # "cuda"/"mps"/"cpu"
    task_category: str = "文本转语音"  # 工作夹名后缀（固定中文）
    emotions: dict = field(default_factory=dict)  # {句子文本: 情绪 key}
    auto_accept_reviews: bool = False  # 自动通过人工审核（挂机批量）
    tts_model_dir: str = ""


@dataclass
class TtsRunResult:
    """文本转语音完成后的产物信息（finished_ok 信号负载）。"""

    audio_path: str
    srt_path: str                # 未勾选生成字幕时为空串
    workspace: Workspace


class _TtsCancelled(Exception):
    """用户取消（在各阶段边界与进度回调中抛出）。"""


class TtsRunner(QThread):
    """文本转语音编排线程（信号接口与 PipelineRunner 一致）。

    语音复核为独立人工暂停点：runner 在工作线程发射 review_requested 后
    阻塞在 threading.Event，UI 线程弹对话框并调用 submit_review() 回传
    结果；回传 None / accepted=False 视为取消，已完成阶段的产物保留
    （manifest 记录），下次运行自动复用。

    信号：
        step_state_changed(str, str): 步骤状态变化（step_id, active/done）
        progress_changed(int): 总进度 0-100
        log_emitted(str): 追加日志
        review_requested(object): 请求语音复核（ReviewPayload，kind=tts_voice）
        finished_ok(object): 全部完成（TtsRunResult）
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

    def __init__(
        self, config: TtsTaskConfig,
        workspace: Workspace | None = None, parent=None,
    ) -> None:
        super().__init__(parent)
        self._cfg = config
        self._ws = workspace or Workspace(
            config.output_dir, config.task_name,
            category=config.task_category, stem=config.task_name,
        )
        self._cancel = threading.Event()
        self._review_event = threading.Event()
        self._review_result: ReviewResult | None = None
        # 进度权重（固定四阶段）
        self._weights: list[tuple[str, float]] = [
            (STAGE_TTS_TEXT, 2.0),
            (STAGE_TTS_SYNTH, 90.0),
            (STAGE_TTS_VOICE_REVIEW, 1.0),
            (STAGE_TTS_MUX, 7.0),
        ]
        self._total_weight = sum(w for _s, w in self._weights)
        self._progress_base = 0.0
        self._last_log_message = ""

    # ------------------------------------------------------------------ 对外

    def submit_review(self, result: ReviewResult | None) -> None:
        """UI 线程回传复核结果；None 或 accepted=False 取消任务。"""
        self._review_result = result
        self._review_event.set()

    def cancel(self) -> None:
        """请求取消（合成中的引擎在进度回调检查点退出，worker 随 unload 结束；
        复核暂停中同时唤醒等待，按已取消收场）。"""
        self._cancel.set()
        self._review_event.set()

    @property
    def workspace(self) -> Workspace:
        return self._ws

    # ------------------------------------------------------------------ 线程体

    def run(self) -> None:
        try:
            result = self._execute()
            self.finished_ok.emit(result)
        except _TtsCancelled:
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 - 统一上报 UI
            if self._cancel.is_set():
                self.cancelled.emit()
            else:
                logger.exception("文本转语音任务失败")
                self.failed.emit(f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ 主流程

    def _execute(self) -> TtsRunResult:  # noqa: C901 - 四阶段主流程
        cfg = self._cfg
        self._log(tr("tts_runner.started", name=cfg.task_name))

        # 流程布局版本：三阶段（03_封装输出）→ 四阶段（03_语音复核 +
        # 04_封装输出）后旧工作夹全量重跑（产物目录已变）
        if self._ws.stage_flag("tts_flow_version") != TTS_FLOW_VERSION:
            self._ws.reset_stages(list(TTS_TASK_STAGE_ORDER))
            self._ws.set_stage_flag("tts_flow_version", TTS_FLOW_VERSION)

        # 设置快照对比：影响合成结果的输入变化 → 四阶段全量重跑。
        # 语言单独对比（auto 识别结果回写 cfg 后与原始选择不一致，不能
        # 直接进指纹）：实际合成语言变化同样全量重跑
        fp = self._input_fingerprint()
        if self._ws.stage_flag("tts_input_fp") != fp:
            self._ws.reset_stages(list(TTS_TASK_STAGE_ORDER))
            self._ws.set_stage_flag("tts_input_fp", fp)
        lang_effective = self._effective_lang()
        recorded_lang = self._ws.stage_flag("tts_synth_lang")
        if lang_effective and recorded_lang and lang_effective != recorded_lang:
            self._ws.reset_stages(list(TTS_TASK_STAGE_ORDER))
        # 仅影响成品形态的设置变化 → 只重跑封装输出
        if self._ws.stage_flag("tts_output_fmt") != cfg.output_format:
            self._ws.reset_stages([STAGE_TTS_MUX])
        if bool(self._ws.stage_flag("tts_srt_enabled")) != cfg.generate_srt:
            self._ws.reset_stages([STAGE_TTS_MUX])
        self._check()

        # 1. 文本处理：分句 + 语言检测 + 参考音频规范化
        self._stage_begin(STAGE_TTS_TEXT)
        if self._ws.is_done(STAGE_TTS_TEXT):
            data = self._read_json(self._ws.tts_sentences_json)
            sentences = data.get("sentences") or []
            lang = str(data.get("lang") or cfg.lang_code or "zh")
            cfg.lang_code = lang  # 回写任务配置（SRT 命名用，与 ASR 惯例一致）
            self._skip_stage(STAGE_TTS_TEXT)
        else:
            sentences = [
                {"text": s, "emo": cfg.emotions.get(s)}
                for s in split_sentences(cfg.text)
            ]
            if not sentences:
                raise RuntimeError(tr("tts_runner.no_text"))
            lang = self._resolve_language(sentences)
            cfg.lang_code = lang
            self._normalize_ref_audio()
            self._write_json(self._ws.tts_sentences_json, {
                "lang": lang,
                "sentences": [
                    {"text": s["text"], "emo": s["emo"] or None}
                    for s in sentences
                ],
            })
            self._ws.set_stage_flag("tts_synth_lang", lang)
            self._ws.mark_done(STAGE_TTS_TEXT)
            self._stage_end(STAGE_TTS_TEXT)
        self._check()
        self._log(tr("tts_runner.sentences", count=len(sentences),
                     lang=self._lang_name(lang)))

        # 2. 语音合成（逐句克隆，断点按句粒度复用；引擎保持加载供复核复用）
        self._stage_begin(STAGE_TTS_SYNTH)
        tts_ctx: dict = {}
        if self._ws.is_done(STAGE_TTS_SYNTH):
            data = self._read_json(self._ws.tts_synth_json)
            entries = data.get("entries") or []
            self._skip_stage(STAGE_TTS_SYNTH)
        else:
            entries, tts_ctx = self._run_synth(sentences, lang)
        self._check()
        if len(entries) != len(sentences):
            raise RuntimeError(tr("tts_runner.synth_incomplete"))

        # 3. 语音复核（试听/删除/重生成，独立暂停点；产物存 03_语音复核）
        self._stage_begin(STAGE_TTS_VOICE_REVIEW)
        if self._ws.is_done(STAGE_TTS_VOICE_REVIEW):
            reviewed = self._read_json(
                self._ws.tts_voice_reviewed_json,
            ).get("entries") or []
            self._skip_stage(STAGE_TTS_VOICE_REVIEW)
        else:
            reviewed = self._run_voice_review(sentences, entries, tts_ctx, lang)
            # 复核改过删除标记/重生成时长：封装输出产物失效重跑
            if any(
                bool(row.get("deleted"))
                or float(row.get("duration") or 0.0)
                != float(entries[i].get("duration") or 0.0)
                for i, row in enumerate(reviewed)
            ):
                self._ws.reset_stages([STAGE_TTS_MUX])
        self._check()
        # 复核阶段结束：TTS 引擎统一释放（合成与复核两阶段共用）
        self._release_tts_engine(tts_ctx)

        # 4. 封装输出：拼接复核保留句 + 静音 + 转码 + SRT，成品直落 04_封装输出 夹
        self._stage_begin(STAGE_TTS_MUX)
        srt_out = ""
        if self._ws.is_done(STAGE_TTS_MUX):
            rel = str(self._ws.stage_flag("tts_final_audio") or "")
            audio_out = str(self._ws.dir / rel) if rel else ""
            if self._ws.stage_flag("tts_srt_enabled"):
                srt_out = str(self._ws.tts_srt_path(lang))
            self._skip_stage(STAGE_TTS_MUX)
        else:
            kept = [r for r in reviewed if not r.get("deleted")]
            if not kept:
                raise RuntimeError(tr("tts_runner.all_deleted"))
            audio_out, srt_out = self._run_mux(kept, lang)
        self._check()

        self.progress_changed.emit(100)
        return TtsRunResult(
            audio_path=audio_out, srt_path=srt_out, workspace=self._ws,
        )

    # ------------------------------------------------------------------ 阶段实现

    def _resolve_language(self, sentences: list[dict]) -> str:
        """确定合成语言：自动识别走 langid（混排判 zhen），回写并记录日志。"""
        cfg = self._cfg
        if cfg.lang_code != "auto":
            return cfg.lang_code

        from app.core.langid_util import (
            detect_text_language,
            is_tts_supported,
            language_display_name,
        )

        sample = "".join(s["text"] for s in sentences)
        detected = detect_text_language(sample)
        if not detected:
            self._log(tr("tts_runner.lang_detect_failed"))
            return "zh"
        self._log(tr("runner.lang_detected",
                     lang=language_display_name(detected)))
        if not is_tts_supported(detected):
            # 不支持语种：窗口启动前已确认过，这里记录回退行为
            self._log(tr("tts_runner.lang_fallback",
                         lang=language_display_name(detected)))
        return detected

    def _normalize_ref_audio(self) -> None:
        """参考音频规范化（模块级 normalize_ref_audio 的薄封装）。"""
        normalize_ref_audio(Path(self._cfg.ref_audio_path), self._ws.tts_ref_audio)

    def _run_synth(self, sentences: list[dict], lang: str) -> tuple[list[dict], dict]:
        """逐句合成（自由时长），返回 (逐句结果, 复核上下文)；句级断点复用。

        合成完成后引擎保持加载（语音复核的即时重生成复用，避免重复
        加载 10GB 级模型），由复核阶段结束后统一释放；异常/取消时就地
        卸载后上抛。
        """
        from app.pipeline.tts import TTSEngine, TTSLoadMemoryError

        cfg = self._cfg
        wav_dir = self._ws.tts_wav_dir
        wav_dir.mkdir(parents=True, exist_ok=True)
        progress = self._stage_progress(STAGE_TTS_SYNTH)

        # 上次中断的残留产物：句子 WAV 存在且有记录时长的直接复用
        # （指纹一致才会走到这里，残留 WAV 与当前句子集合一一对应）
        total = len(sentences)
        durations: list[float | None] = [None] * total
        if self._ws.tts_synth_json.is_file():
            try:
                data = self._read_json(self._ws.tts_synth_json)
                for i, entry in enumerate(data.get("entries") or []):
                    if i >= total:
                        break
                    rel = str(entry.get("file") or "")
                    if rel and (self._ws.dir / rel).is_file():
                        durations[i] = float(entry.get("duration") or 0.0)
            except (OSError, ValueError):
                durations = [None] * total

        def _synth_msg(idx: int, frac: float) -> str:
            """句内进度消息：10% 档位量化。0% 档与句开始消息相同，交由
            去重机制吞掉；其余档位在日志区逐级显示（每档仅一条）。"""
            pct = int(frac * 100) // 10 * 10
            if pct <= 0:
                return tr("tts_runner.synth_progress",
                          done=idx + 1, total=total)
            return tr("tts_runner.synth_progress_pct",
                      done=idx + 1, total=total, pct=pct)

        # 阶段内进度布局：模型加载占前 10%，逐句合成占后 90%，
        # 两段单调衔接 —— 消除加载收尾报 1.0 后第一句从 0 重爬的回退
        def _load_progress(fraction: float, message: str) -> None:
            progress(min(fraction, 1.0) * 0.1, message)

        def _synth_progress(fraction: float, message: str) -> None:
            progress(0.1 + min(fraction, 1.0) * 0.9, message)

        def _synth_all(engine) -> None:
            for i, sent in enumerate(sentences):
                self._check()
                out = wav_dir / f"{i:04d}.wav"
                if durations[i] is None:
                    # 句开始即打日志（确认任务已开工）；句内进度只用于日志
                    # 的 10% 档位显示，进度条保持句粒度推进（idx/total），
                    # 不再随句内模拟锚点连续推进
                    _synth_progress(
                        i / total,
                        tr("tts_runner.synth_progress",
                           done=i + 1, total=total),
                    )
                    duration, _ratio = engine.synthesize(
                        sent["text"], str(self._ws.tts_ref_audio),
                        0.0, str(out),  # 自由时长：目标时长 0 = 不对齐
                        lang=lang,
                        emo_vector=emotion_vector(sent["emo"]),
                        duration_factor=1.0 / max(cfg.speed, 0.1),
                        progress=lambda frac, _desc="", idx=i: _synth_progress(
                            idx / total,
                            _synth_msg(idx, frac),
                        ),
                    )
                    durations[i] = round(duration, 3)
                    # 句完成：进度条推进一格，日志补 100% 档与句内
                    # 90% 档衔接成完整序列
                    _synth_progress(
                        (i + 1) / total,
                        tr("tts_runner.synth_progress_pct",
                           done=i + 1, total=total, pct=100),
                    )
            _synth_progress(
                1.0,
                tr("tts_runner.synth_progress_pct",
                   done=total, total=total, pct=100),
            )

        engine = TTSEngine()
        try:
            try:
                engine.load(cfg.tts_model_dir, device=cfg.device,
                            progress=_load_progress)
                _synth_all(engine)
            except Exception as exc:
                # GPU 失败换 CPU 重试（已完成句子的 WAV 已落盘，重试不做
                # 重复工作）；内存不足与用户取消不重试，直接上抛
                if cfg.device == "cpu" or isinstance(
                    exc, (TTSLoadMemoryError, _TtsCancelled)
                ):
                    raise
                self._log(tr("runner.device_fallback",
                             stage=tr("step.tts_synth"), error=exc))
                engine.unload()
                engine.load(cfg.tts_model_dir, device="cpu",
                            progress=_load_progress)
                _synth_all(engine)
            result = [
                {
                    "file": (wav_dir / f"{i:04d}.wav")
                    .relative_to(self._ws.dir).as_posix(),
                    "duration": durations[i],
                }
                for i in range(total)
            ]
            self._write_json(self._ws.tts_synth_json, {"entries": result})
            self._ws.mark_done(STAGE_TTS_SYNTH)
            self._stage_end(STAGE_TTS_SYNTH)
            # 引擎保持加载：语音复核的即时重生成复用（复核阶段结束后释放）
            return result, {"engine": engine}
        except BaseException:
            engine.unload()  # 合成中断：引擎无人接手释放，就地卸载
            raise

    def _run_mux(self, rows: list[dict], lang: str) -> tuple[str, str]:
        """拼接句间静音 → 转码到目标格式 → 写 SRT；成品直落 04_封装输出 夹。

        rows 为语音复核保留的句子（已过滤删除标记，含 file/duration/text）；
        时间轴 = 各句实际时长顺序累计（绝不漂移）。
        """
        import numpy as np
        import soundfile as sf

        cfg = self._cfg
        fmt = cfg.output_format
        final_audio = self._ws.tts_audio_path(fmt)
        stage_dir = self._ws.ensure_stage_dir(STAGE_TTS_MUX)

        # 拼接：句间固定静音，时间轴 = 各句实际时长顺序累计（绝不漂移）
        timeline: list[tuple[float, float]] = []
        pieces: list[np.ndarray] = []
        silence = np.zeros(int(INTERVAL_SILENCE_SEC * TTS_SAMPLE_RATE),
                           dtype="float32")
        cursor = 0.0
        for i, row in enumerate(rows):
            wav, sr = sf.read(
                str(self._ws.dir / row["file"]), dtype="float32",
                always_2d=True,
            )
            if sr != TTS_SAMPLE_RATE:
                raise RuntimeError(tr("tts_runner.sample_rate_mismatch", sr=sr))
            piece = wav[:, 0]
            duration = len(piece) / sr
            timeline.append((cursor, cursor + duration))
            pieces.append(piece)
            cursor += duration + INTERVAL_SILENCE_SEC
            if i < len(rows) - 1:
                pieces.append(silence)
        full = np.concatenate(pieces) if pieces else np.zeros(0, "float32")

        # 封装输出：WAV 直接写成品（免中转复制）；其余格式先落中转文件，
        # 经内置 ffmpeg 转码后删除中转
        if fmt == "wav":
            sf.write(str(final_audio), full, TTS_SAMPLE_RATE, subtype="PCM_16")
        else:
            combined = stage_dir / "combined.tmp.wav"
            sf.write(str(combined), full, TTS_SAMPLE_RATE, subtype="PCM_16")
            from app.core.ffmpeg import get_ffmpeg_path

            ffmpeg = get_ffmpeg_path()
            if not ffmpeg:
                combined.unlink(missing_ok=True)
                raise RuntimeError(tr("tts_runner.ffmpeg_missing"))
            cmd = [ffmpeg, "-y", "-i", str(combined), *_FORMAT_CODECS[fmt],
                   str(final_audio)]
            proc = subprocess.run(cmd, capture_output=True, timeout=600,
                                  check=False)
            combined.unlink(missing_ok=True)  # 转码结束即清中转文件
            if proc.returncode != 0:
                detail = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
                raise RuntimeError(tr("tts_runner.format_failed",
                                      fmt=fmt, error=detail))
        self._log(tr("tts_runner.audio_done", path=final_audio.name,
                     seconds=round(cursor - INTERVAL_SILENCE_SEC, 1)))

        # SRT 字幕：一条 SRT 块 = 一个合成单位（长句模型内部分块不影响整句块）
        srt_out = ""
        if cfg.generate_srt:
            srt_out = str(self._ws.tts_srt_path(lang))
            segs = []
            for row, (start, end) in zip(rows, timeline):
                text = row["text"]
                segs.append(Segment(
                    start=round(start, 3), end=round(end, 3),
                    text=text, target_text=text,
                ))
            generate_srt(segs, srt_out, MODE_MONO, target_lang=lang)
            self._log(tr("runner.srt_done",
                         path=Path(srt_out).name, count=len(segs)))

        # manifest 标记（is_done 校验 + 下次运行设置对比）
        self._ws.set_stage_flag("tts_output_fmt", fmt)
        self._ws.set_stage_flag("tts_srt_enabled", bool(cfg.generate_srt))
        self._ws.set_stage_flag(
            "tts_final_audio",
            final_audio.relative_to(self._ws.dir).as_posix(),
        )
        self._ws.mark_done(STAGE_TTS_MUX)
        self._stage_end(STAGE_TTS_MUX)
        return str(final_audio), srt_out

    # ------------------------------------------------------------------ 语音复核

    def _run_voice_review(
        self, sentences: list[dict], entries: list[dict],
        tts_ctx: dict, lang: str,
    ) -> list[dict]:
        """语音复核（独立暂停点）：试听全部句子，可删除与即时重生成。

        复核行 = 分句 + 合成记录（text/emo/file/duration/deleted）；重生成
        覆盖原 WAV 并回写新时长。引擎与合成阶段共用：合成后保持加载，
        合成被跳过（复用产物）时按需加载；复核未决期间 runner 线程阻塞
        等待结果，对话框后台线程经 regen_hook 使用引擎不会并发。复核
        标记被重置后重入：合成结果未变时保留上次的删除标记。删除句的
        WAV 保留（可恢复），仅不进入成品。结果存 03_语音复核/reviewed.json。
        """
        from app.pipeline.tts import TTSEngine, tts_runtime_ready

        cfg = self._cfg
        prev_deleted = self._load_prev_deletions(sentences, entries)
        rows = [
            {
                "text": sent["text"],
                "emo": sent["emo"],
                "file": str(entry["file"]),
                "duration": float(entry["duration"] or 0.0),
                "deleted": bool(prev_deleted.get(str(entry["file"]), False)),
            }
            for sent, entry in zip(sentences, entries)
        ]

        if cfg.auto_accept_reviews:
            self._log(tr("tts_runner.review_auto"))
        else:
            engine = tts_ctx.get("engine")
            if engine is None:
                # 复核可能即时重生成：预加载引擎（复核期间保持加载）
                ok, detail = tts_runtime_ready()
                if not ok:
                    raise RuntimeError(tr("runner.tts_env_missing", detail=detail))
                engine = TTSEngine()
                try:
                    engine.load(cfg.tts_model_dir, device=cfg.device,
                                progress=self._log_progress())
                except Exception as exc:
                    if cfg.device == "cpu" or isinstance(exc, _TtsCancelled):
                        raise
                    self._log(tr("runner.device_fallback",
                                 stage=tr("step.tts_synth"), error=exc))
                    engine.load(cfg.tts_model_dir, device="cpu",
                                progress=self._log_progress())
                tts_ctx["engine"] = engine

            self._log(tr("tts_runner.review_start"))

            def _regen_one(local_idx: int, progress=None) -> float:
                """对话框内即时重生成一句（覆盖原 WAV，回写新时长）。"""
                row = rows[local_idx]
                duration, _ratio = engine.synthesize(
                    row["text"], str(self._ws.tts_ref_audio), 0.0,
                    str(self._ws.dir / row["file"]),
                    lang=lang,
                    emo_vector=emotion_vector(row["emo"]),
                    duration_factor=1.0 / max(cfg.speed, 0.1),
                    progress=progress,
                )
                row["duration"] = round(duration, 3)
                return row["duration"]

            try:
                self._pause_for_review(ReviewPayload(
                    kind="tts_voice", segments=rows, regen_hook=_regen_one,
                ))
            except BaseException:
                self._release_tts_engine(tts_ctx)
                raise

        kept = sum(1 for row in rows if not row["deleted"])
        self._log(tr("tts_runner.review_done",
                     kept=kept, deleted=len(rows) - kept))
        self._write_json(self._ws.tts_voice_reviewed_json, {"entries": rows})
        self._ws.mark_done(STAGE_TTS_VOICE_REVIEW)
        self._stage_end(STAGE_TTS_VOICE_REVIEW)
        return rows

    def _load_prev_deletions(
        self, sentences: list[dict], entries: list[dict],
    ) -> dict[str, bool]:
        """重入复核（标记被重置）时保留上次的删除标记。

        仅当逐句合成结果未变（文件/时长/文本一致）时复用；合成重跑后
        上次复核不再适用，按全部未删除处理。
        """
        try:
            prev = self._read_json(
                self._ws.tts_voice_reviewed_json,
            ).get("entries") or []
        except (OSError, ValueError):
            return {}
        if len(prev) != len(entries):
            return {}
        for sent, entry, old in zip(sentences, entries, prev):
            if (
                str(old.get("file") or "") != str(entry["file"])
                or str(old.get("text") or "") != str(sent["text"])
                or round(float(old.get("duration") or 0.0), 3)
                != round(float(entry["duration"] or 0.0), 3)
            ):
                return {}
        return {str(p.get("file") or ""): bool(p.get("deleted")) for p in prev}

    # ------------------------------------------------------------------ 快照/工具

    def _input_fingerprint(self) -> str:
        """合成输入结构指纹：文本/语速/情绪/参考音频/模型目录任一变化即失效。

        语言不进指纹（auto 识别结果会回写 cfg.lang_code，重试同一配置
        对象时与原始选择不一致），由 _effective_lang 与 tts_synth_lang
        标记单独对比。
        """
        cfg = self._cfg
        payload = json.dumps({
            "text": cfg.text,
            "speed": float(cfg.speed),
            "emotions": {k: v for k, v in sorted(cfg.emotions.items())},
            "ref": str(Path(cfg.ref_audio_path).resolve()),
            "model": cfg.tts_model_dir,
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()

    def _effective_lang(self) -> str:
        """实际合成语言：显式选择优先；auto 时取既有识别缓存（无则空）。"""
        if self._cfg.lang_code != "auto":
            return self._cfg.lang_code
        try:
            if self._ws.tts_sentences_json.is_file():
                return str(
                    self._read_json(self._ws.tts_sentences_json).get("lang") or ""
                )
        except (OSError, ValueError):
            pass
        return ""

    def _lang_name(self, code: str) -> str:
        from app.core.langid_util import language_display_name

        return language_display_name(code)

    def _read_json(self, path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))

    def _write_json(self, path: Path, data: dict) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------ 暂停/取消

    @staticmethod
    def _release_tts_engine(tts_ctx: dict) -> None:
        """释放复核上下文中保持加载的 TTS 引擎（无引擎时无操作）。"""
        engine = tts_ctx.get("engine")
        if engine is not None:
            engine.unload()
            tts_ctx["engine"] = None

    def _pause_for_review(self, payload: ReviewPayload) -> ReviewResult:
        """发射复核请求并阻塞等待 UI 回传；取消时抛 _TtsCancelled。"""
        self._review_event.clear()
        self._review_result = None
        self.review_requested.emit(payload)
        self._review_event.wait()
        self._check()  # cancel() 唤醒时在此抛出
        result = self._review_result
        if result is None or not result.accepted:
            raise _TtsCancelled()
        return result

    def _check(self) -> None:
        """取消检查点。"""
        if self._cancel.is_set():
            raise _TtsCancelled()

    def _weight_span(self, stage: str) -> tuple[float, float]:
        base = 0.0
        for name, weight in self._weights:
            if name == stage:
                return base, base + weight
            base += weight
        return base, base

    def _stage_begin(self, stage: str) -> None:
        self.step_state_changed.emit(stage, "active")

    def _stage_end(self, stage: str) -> None:
        self._progress_base = self._weight_span(stage)[1]
        self.step_state_changed.emit(stage, "done")
        self.progress_changed.emit(
            int(self._progress_base / self._total_weight * 100)
        )

    def _skip_stage(self, stage: str) -> None:
        """跳过已完成阶段（复用产物）。"""
        self._log(tr("runner.stage_skipped", stage=tr(f"step.{stage}")))
        self._stage_end(stage)

    def _stage_progress(self, stage: str) -> ProgressCallback:
        runner = self
        start, end = self._weight_span(stage)

        def _callback(fraction: float, message: str) -> None:
            runner._check()
            fraction = min(max(fraction, 0.0), 1.0)
            runner.progress_changed.emit(
                int((start + (end - start) * fraction)
                    / runner._total_weight * 100)
            )
            if message and message != runner._last_log_message:
                runner._last_log_message = message
                runner.log_emitted.emit(message)

        return _callback

    def _log_progress(self) -> ProgressCallback:
        """转发日志消息、不影响进度条的回调（复核前引擎加载可见状态）。"""
        runner = self

        def _callback(_fraction: float, message: str) -> None:
            runner._check()
            if message:
                runner.log_emitted.emit(message)

        return _callback

    def _log(self, message: str) -> None:
        self.log_emitted.emit(message)
