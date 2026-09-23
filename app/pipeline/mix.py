# -*- coding: utf-8 -*-
"""音频混音模块：TTS 配音轨响度对齐后叠加到背景（无人声）轨。

- 响度测量/对齐：numpy + soundfile 实现 RMS（dBFS），不依赖 ffmpeg loudnorm
  （避免两次进程调用）；
- 混音：以 no_vocals 为基准轨（采样率/总时长/声道数），TTS 逐条重采样、
  响度对齐到原人声参考后按 start 时间叠加；
- stretch_to_duration：librosa 变调不变速时长拉伸，供 TTS 模块兜底。
"""

from __future__ import annotations

import logging
import math
from pathlib import Path

import numpy as np
import soundfile as sf

logger = logging.getLogger(__name__)

# 延迟引用共享结构：app/pipeline/base.py 可能由其他模块并行开发
try:
    from app.pipeline.base import ProgressCallback
except Exception:  # pragma: no cover - base.py 尚未就绪时兜底
    ProgressCallback = None  # type: ignore[assignment,misc]

# 静音/近静音的 RMS 下限（dBFS），避免 log10(0) 与无穷增益
SILENCE_DB = -120.0


def _rms_db(data: np.ndarray) -> float:
    """float 音频数组（任意形状，取全部样本）的综合 RMS（dBFS）。"""
    if data is None or data.size == 0:
        return SILENCE_DB
    # 用 float64 累加，避免长音频 float32 溢出/精度损失
    rms = float(np.sqrt(np.mean(np.square(data, dtype=np.float64))))
    if rms <= 0.0:
        return SILENCE_DB
    return 20.0 * math.log10(rms)


def _report(progress, fraction: float, message: str) -> None:
    """安全上报进度（progress 为 None 时忽略，fraction 夹到 [0, 1]）。"""
    if progress is not None:
        progress(min(max(fraction, 0.0), 1.0), message)


def measure_loudness(wav_path: str) -> float:
    """测量 wav 综合响度：全部声道全部样本的 RMS（dBFS）。

    numpy + soundfile 实现（不引入 ffmpeg loudnorm，避免两次进程调用）。
    静音返回 SILENCE_DB（-120）。
    """
    data, _sr = sf.read(wav_path, dtype="float64", always_2d=True)
    return _rms_db(data)


def align_loudness(wav_path: str, target_db: float, out_path: str | None = None) -> str:
    """把整条音轨 RMS 对齐到 target_db（dBFS）。

    计算增益（target_db - 当前 RMS dB）后 numpy 相乘，clip ±1.0 防溢出写出；
    out_path 为 None 时原地覆盖；返回写出的文件路径。
    """
    info = sf.info(wav_path)
    data, sr = sf.read(wav_path, dtype="float32", always_2d=True)
    cur_db = _rms_db(data)
    if cur_db > SILENCE_DB:
        gain = 10.0 ** ((target_db - cur_db) / 20.0)
        data = np.clip(data * gain, -1.0, 1.0)
    else:
        logger.warning("音轨近静音（RMS %.1f dBFS），跳过响度对齐: %s", cur_db, wav_path)
    dest = out_path if out_path else wav_path
    sf.write(dest, data, sr, subtype=info.subtype, format=info.format)
    return dest


def _load_tts_as_track(path: str, canvas_sr: float, canvas_channels: int) -> np.ndarray:
    """读入 TTS 音频并整形为 (n, canvas_channels) float32。

    采样率与画布不同 → librosa.resample；声道数与画布不同 →
    先转单声道（多声道取均值）再复制到画布声道数。
    """
    y, sr_t = sf.read(path, dtype="float32", always_2d=True)
    if sr_t != canvas_sr:
        import librosa  # 局部导入：仅重采样路径需要，缩短模块首次加载

        # (n, ch) → 时间在 axis=0，librosa 沿时间轴重采样
        y = librosa.resample(y, orig_sr=sr_t, target_sr=canvas_sr, axis=0)
    if y.shape[1] == 1:
        mono = y[:, 0]
    else:
        mono = y.mean(axis=1)
    if canvas_channels == 1:
        return mono.reshape(-1, 1)
    return np.repeat(mono.reshape(-1, 1), canvas_channels, axis=1)


def mix_tracks(
    tts_segments: list,
    no_vocals_wav: str,
    out_wav: str,
    vocals_ref_wav: str,
    progress: "ProgressCallback | None" = None,
) -> str:
    """把 TTS 配音段叠加到背景轨，写出成品混音 wav（pcm_s16le），返回 out_wav。

    tts_segments：每项含 start/end/tts_audio_path 属性的对象列表（duck typing）。
    流程：
    ① 读 no_vocals 得总时长与采样率（以 no_vocals 为基准轨）；
    ② 测量 vocals_ref_wav（原人声）综合 RMS 作为响度参考；
    ③ 创建等长静音画布（float32，同背景轨声道数）；
    ④ 逐条：读 TTS（重采样/声道整形）→ RMS 对齐到原人声响度 → 按 start
      叠加进画布（越界裁剪，与已有内容相加）；
    ⑤ 画布 + no_vocals 相加、clip ±1.0、写出 out_wav。
    tts_audio_path 为空或文件不存在的段跳过并 warning。
    """
    # ① 基准轨
    bg, sr = sf.read(no_vocals_wav, dtype="float32", always_2d=True)
    total_samples, channels = bg.shape
    # ② 原人声参考响度
    ref_db = measure_loudness(vocals_ref_wav)
    logger.info(
        "混音开始: 基准轨 %.3fs / %dHz / %dch, 人声参考 RMS %.2f dBFS",
        total_samples / sr, sr, channels, ref_db,
    )
    # ③ 静音画布
    canvas = np.zeros_like(bg)
    n = len(tts_segments) if tts_segments else 0
    done = 0
    for seg in tts_segments:
        start = float(getattr(seg, "start", 0.0))
        end = float(getattr(seg, "end", 0.0))
        path = getattr(seg, "tts_audio_path", "") or ""
        if not path or not Path(path).is_file():
            logger.warning(
                "跳过无 TTS 音频的分段: start=%.3f end=%.3f text=%r",
                start, end, str(getattr(seg, "text", ""))[:30],
            )
            continue
        # ④ 读入、整形、对齐响度
        track = _load_tts_as_track(path, sr, channels)
        seg_db = _rms_db(track)
        if seg_db > SILENCE_DB:
            gain = 10.0 ** ((ref_db - seg_db) / 20.0)
            track = track * gain
        else:
            logger.warning("TTS 段近静音，未做响度增益: %s", path)
        # 按时间叠加（越界裁剪：头部负偏移与尾部超出画布都裁掉）
        start_sample = int(round(start * sr))
        src_from = max(0, -start_sample)
        dst_from = max(0, start_sample)
        dst_to = min(total_samples, start_sample + len(track))
        if dst_to <= dst_from:
            logger.warning("TTS 段完全落在画布外，跳过: start=%.3f", start)
            continue
        canvas[dst_from:dst_to] += track[src_from : src_from + (dst_to - dst_from)]
        done += 1
        _report(progress, done / max(n, 1), f"混音配音段 {done}/{n}")
    # ⑤ 与背景轨合并
    mixed = np.clip(bg + canvas, -1.0, 1.0)
    _report(progress, 0.98, "写出混音文件")
    sf.write(out_wav, mixed, sr, subtype="PCM_16")
    _report(progress, 1.0, "混音完成")
    return out_wav


def stretch_to_duration(wav_path: str, target_dur: float, out_path: str) -> float:
    """把音频变调不变速地拉伸/压缩到目标时长，返回实际输出时长（秒）。

    librosa.effects.time_stretch，rate = 原时长 / 目标时长
    （rate < 1 减速拉长，rate > 1 加速缩短）。供 TTS 模块超长时兜底。
    """
    y, sr = sf.read(wav_path, dtype="float32", always_2d=True)
    orig_dur = len(y) / sr
    if target_dur <= 0 or orig_dur <= 0:
        sf.write(out_path, y, sr, subtype="PCM_16")
        return orig_dur
    rate = orig_dur / target_dur
    if abs(rate - 1.0) < 1e-6:
        stretched = y
    else:
        import warnings

        import librosa

        # time_stretch 期望时间在最后一维：(n, ch) → (ch, n) → 拉伸 → 转回。
        # librosa 1.0 的 effects→phase_vocoder 链会自行触发 FutureWarning；
        # 近静音帧的相位计算可能产生 NaN（numba 转换告警），此处静音并清洗输出。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            stretched = librosa.effects.time_stretch(y.T, rate=rate).T.astype(np.float32)
        stretched = np.nan_to_num(stretched, nan=0.0, posinf=0.0, neginf=0.0)
    out_dur = len(stretched) / sr
    sf.write(out_path, stretched, sr, subtype="PCM_16")
    return out_dur
