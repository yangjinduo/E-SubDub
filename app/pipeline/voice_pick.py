# -*- coding: utf-8 -*-
"""参考音色挑选：为每个说话人从人声轨中挑一条高质量片段作为 TTS 音色参考。"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf

from app.core.ffmpeg import get_ffmpeg_path
from app.core.segment import Segment
from app.pipeline.base import ProgressCallback, report_progress

# 参考音频分析帧长（秒）
_FRAME_SEC = 0.05
# 候选数量：文本最长的前 N 条进入音质评估
_TOP_CANDIDATES = 3


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
) -> dict[str, str]:
    """为每个说话人挑参考音色片段，返回 {spk: ref_wav_path}。

    - 候选 = 未删除、时长在 [min_dur, max_dur] 的分段，取文本最长的前 3 条；
    - 从候选中选音频质量最好的一条（RMS 适中且波动小）；
    - 用 ffmpeg 切出 work_dir/ref_{spk}.wav（16kHz 单声道）。
    - 无候选的说话人不出现在返回值中。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)

    by_spk: dict[str, list[Segment]] = {}
    for seg in segments:
        if seg.deleted:
            continue
        if not (min_dur <= seg.duration <= max_dur):
            continue
        by_spk.setdefault(seg.spk, []).append(seg)

    refs: dict[str, str] = {}
    spk_list = sorted(by_spk.keys())
    for i, spk in enumerate(spk_list):
        # 文本较长的 top 3 候选（文本长 -> 语音信息多）
        candidates = sorted(by_spk[spk], key=lambda s: len(s.text), reverse=True)[:_TOP_CANDIDATES]
        best, best_score = None, float("-inf")
        for seg in candidates:
            score = _quality_score(vocals_wav, seg.start, seg.end)
            if score > best_score:
                best, best_score = seg, score
        if best is None:
            continue

        out_path = work_dir / f"ref_{spk}.wav"
        _cut_reference(vocals_wav, best.start, best.duration, out_path)
        refs[spk] = str(out_path)
        report_progress(
            progress,
            (i + 1) / len(spk_list),
            f"已生成 {spk} 参考音色（{best.start:.1f}~{best.end:.1f} 秒）",
        )
    return refs
