# -*- coding: utf-8 -*-
"""参考音色挑选：说话人级参考（音色基准）+ 逐句切片（语气跟随）。

说话人级：为每个说话人从人声轨中挑一条高质量片段作为 TTS 音色基准；
逐句切片：为每个待合成句切出其自身的人声片段，存入人工审核阶段的
"句子切片"文件夹（命名 开始时间-结束时间-说话人ID-文本.wav），并直接
作为该句 TTS 的参考音频——短句也用自身切片（IndexTTS 支持短参考克隆，
可保留原句情绪），仅近无声或切割失败的句子回退说话人级参考。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

from app.core.ffmpeg import get_ffmpeg_path
from app.core.segment import Segment
from app.pipeline.base import ProgressCallback, report_progress

logger = logging.getLogger(__name__)

# 参考音频分析帧长（秒）
_FRAME_SEC = 0.05
# 候选数量：文本最长的前 N 条进入音质评估
_TOP_CANDIDATES = 3
# 兜底候选时长下限（秒）：说话人没有 [min_dur, max_dur] 窗口内的长句时，
# 放宽到 >=1.5 秒；再不行取任意非零时长——宁可选短参考也不能漏掉说话人，
# 否则该说话人在音色管理窗口不出现、配音阶段整段被跳过
_MIN_FALLBACK_SEC = 1.5
# 自身切片近无声判定阈值（与 _quality_score 的 -1e9 / -inf 对应）
_SILENT_SCORE_THRESHOLD = -1e8
# 文件名中的非法字符（Windows 保留字符）
_FILENAME_BAD = '\\/:*?"<>|'
# 切片文件名中文本部分的最大长度（避免超出 Windows 路径长度限制）
_TEXT_NAME_LIMIT = 40


def _safe_text(text: str, limit: int = _TEXT_NAME_LIMIT) -> str:
    """清理文本使其可安全用于文件名：去非法字符/换行，压缩空白并限长。"""
    cleaned = "".join("-" if ch in _FILENAME_BAD else ch for ch in text)
    cleaned = " ".join(cleaned.split())
    return cleaned[:limit].strip() or "无文本"


def sentence_base_name(start: float, end: float, spk: str, text: str) -> str:
    """逐句音频文件基础名：开始时间-结束时间-说话人ID-文本（不含扩展名）。"""
    return f"{start:.2f}-{end:.2f}-{_safe_text(spk, 20)}-{_safe_text(text)}"


def _quality_score(vocals_wav: str, start: float, end: float) -> float:
    """评估 [start, end] 区间的音频质量，分数越高越好。

    用 soundfile 分帧计算 RMS：
    - RMS 均值适中（约 0.005~0.4，过低近乎无声、过高可能削波）；
    - 帧间 RMS 波动小（变异系数 cv = std/mean 越小越稳定）；
    - 有效语音帧占比高（RMS > 0.25 * 95 分位的帧比例）。
    """
    # sf.read 的 start/stop 是帧索引，需先取采样率把秒换算为帧
    sr = sf.info(vocals_wav).samplerate
    data, _ = sf.read(
        vocals_wav,
        start=max(0, int(start * sr)),
        stop=int(end * sr),
        dtype="float32",
        always_2d=True,
    )
    mono = data.mean(axis=1)
    frame_len = max(1, int(_FRAME_SEC * sr))
    n_frames = len(mono) // frame_len
    if n_frames < 2:
        return float("-inf")
    frames = mono[: n_frames * frame_len].reshape(n_frames, frame_len)
    rms = np.sqrt((frames ** 2).mean(axis=1))

    mean = float(rms.mean())
    if mean < 1e-4:
        return -1e9  # 几乎无声
    cv = float(rms.std()) / mean  # 波动越小越好
    peak_ref = float(np.percentile(rms, 95))
    speech_ratio = float((rms > 0.25 * peak_ref).mean()) if peak_ref > 0 else 0.0
    # RMS 适中惩罚：过轻/过响各扣 1 分
    moderate_penalty = 0.0 if 0.005 <= mean <= 0.4 else 1.0
    return speech_ratio - cv - moderate_penalty


def _cut_reference(vocals_wav: str, start: float, duration: float, out_path: Path) -> None:
    """用 ffmpeg 切出参考片段（16kHz 单声道 PCM16，适合 TTS 参考）。"""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("未找到内置 ffmpeg（libs/windows/ffmpeg.exe）")
    cmd = [
        ffmpeg, "-y", "-nostdin",
        "-ss", f"{start:.3f}",
        "-i", vocals_wav,
        "-t", f"{duration:.3f}",
        "-ac", "1",
        "-ar", "16000",
        "-acodec", "pcm_s16le",
        str(out_path),
    ]
    ret = subprocess.run(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    ).returncode
    if ret != 0 or not out_path.is_file():
        raise RuntimeError(f"切参考音频失败: {out_path}")


def pick_reference_segments(
    segments: list[Segment],
    vocals_wav: str,
    work_dir: Path,
    min_dur: float = 3.0,
    max_dur: float = 12.0,
    progress: Optional[ProgressCallback] = None,
) -> tuple[dict[str, str], dict[str, str]]:
    """为每个说话人挑参考音色片段，返回 ({spk: ref_wav_path}, {spk: 参考句文本})。

    - 候选 = 未删除、时长在 [min_dur, max_dur] 的分段，取文本最长的前 3 条；
    - 无窗口内候选的说话人逐级放宽：>=1.5 秒 → 任意非零时长（宁短勿缺）；
    - 从候选中选音频质量最好的一条（RMS 适中且波动小），
      全部得分无效（片段过短）时取时长最长的一条；
    - 用 ffmpeg 切出 work_dir/ref_{spk}.wav（16kHz 单声道）。
    - 参考句文本 = 实际选中分段的原文，供音色管理窗口与试听音频对照
      （不能由界面自行按"最长句"猜测，两者挑选标准不同会导致对不上）。
    - 仅当说话人所有分段均被删除或时长为 0 时才不出现在返回值中。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    by_spk: dict[str, list[Segment]] = {}
    all_spk: dict[str, list[Segment]] = {}
    for seg in segments:
        if seg.deleted or seg.duration <= 0:
            continue
        all_spk.setdefault(seg.spk, []).append(seg)
        if min_dur <= seg.duration <= max_dur:
            by_spk.setdefault(seg.spk, []).append(seg)

    refs: dict[str, str] = {}
    samples: dict[str, str] = {}
    spk_list = sorted(all_spk.keys())
    for i, spk in enumerate(spk_list):
        pool = by_spk.get(spk) or [
            s for s in all_spk[spk] if s.duration >= _MIN_FALLBACK_SEC
        ] or all_spk[spk]
        # 文本较长的 top 3 候选（文本长 -> 语音信息多）
        candidates = sorted(pool, key=lambda s: len(s.text), reverse=True)[:_TOP_CANDIDATES]
        best, best_score = None, float("-inf")
        for seg in candidates:
            score = _quality_score(vocals_wav, seg.start, seg.end)
            if score > best_score:
                best, best_score = seg, score
        if best is None:
            # 片段过短无法评分（<0.1s）时取时长最长的一条
            best = max(candidates, key=lambda s: s.duration)

        out_path = work_dir / f"ref_{spk}.wav"
        _cut_reference(vocals_wav, best.start, best.duration, out_path)
        refs[spk] = str(out_path)
        samples[spk] = str(best.text)
        report_progress(
            progress,
            (i + 1) / len(spk_list),
            f"已生成 {spk} 参考音色（{best.start:.1f}~{best.end:.1f} 秒）",
        )
    return refs, samples


def cut_sentence_slices(
    segments: list[Segment],
    vocals_wav: str,
    spk_refs: dict[str, str],
    out_dir: Path,
    progress: Optional[ProgressCallback] = None,
) -> tuple[dict[int, str], int, int]:
    """逐句切片：为每个待合成句切出其自身的人声片段（完整句长）。

    切片存入人工审核阶段的"句子切片"文件夹（命名
    开始时间-结束时间-说话人ID-文本.wav），并作为该句 TTS 的参考音频，
    使配音继承原句的语气/情绪/节奏。短句也直接用自身切片（短参考克隆
    保留原句情绪）；仅近无声或切割失败时回退说话人级参考。

    返回 ({分段索引: 参考音频路径}, 自身切片句数, 回退句数)；两者皆缺的
    句子不出现在映射中，由调用方跳过。每次重切前清空旧切片，使文件夹
    与当前待合成集合一致（审核删除/改文的句子不再残留旧文件）。
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for old in out_dir.glob("*.wav"):
        try:
            old.unlink()
        except OSError:
            logger.warning("清理旧切片失败: %s", old.name)

    todo = [
        (i, seg) for i, seg in enumerate(segments)
        if not seg.deleted and str(seg.target_text).strip()
    ]
    refs: dict[int, str] = {}
    own = fallback = 0
    total = len(todo)
    for done, (i, seg) in enumerate(todo, 1):
        out_path = out_dir / f"{sentence_base_name(seg.start, seg.end, seg.spk, seg.text)}.wav"
        usable = True
        if _quality_score(vocals_wav, seg.start, seg.end) < _SILENT_SCORE_THRESHOLD:
            usable = False  # 近无声切片无法提供有效的音色/语气信息
        if usable:
            try:
                _cut_reference(vocals_wav, seg.start, seg.duration, out_path)
            except (RuntimeError, OSError) as exc:
                logger.warning("切句失败（回退说话人参考）%s: %r", out_path.name, exc)
                usable = False
            else:
                refs[i] = str(out_path)
                own += 1
        if not usable:
            spk_ref = spk_refs.get(seg.spk)
            if spk_ref:
                refs[i] = spk_ref
                fallback += 1
        report_progress(progress, done / max(total, 1), f"逐句切片 {done}/{total}")
    return refs, own, fallback
