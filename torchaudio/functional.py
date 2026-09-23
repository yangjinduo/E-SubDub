# -*- coding: utf-8 -*-
"""torchaudio.functional 替身：resample（scipy 多相滤波）。"""

from __future__ import annotations

from math import gcd


def resample(waveform, orig_freq, new_freq):
    """按频率比重采样最后一维（…, time），行为对齐 torchaudio.functional.resample。"""
    import torch
    from scipy.signal import resample_poly

    orig_freq, new_freq = int(orig_freq), int(new_freq)
    if orig_freq == new_freq:
        return waveform

    g = gcd(new_freq, orig_freq)
    up, down = new_freq // g, orig_freq // g

    was_tensor = isinstance(waveform, torch.Tensor)
    data = waveform.numpy() if was_tensor else waveform

    resampled = resample_poly(data, up, down, axis=-1)

    if was_tensor:
        resampled = torch.from_numpy(resampled).to(waveform.dtype)
    return resampled
