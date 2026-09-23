# -*- coding: utf-8 -*-
"""torchaudio 极简替身（soundfile + scipy 实现）。

背景：本机 torch 2.14 在 PyPI 上没有配套的 torchaudio 版本（最高 2.11，
且 2.9+ 的 load/save 依赖 TorchCodec）。但 MOSS-TTS / Demucs 的模型代码
顶层 `import torchaudio` 并调用：

- torchaudio.load(path)                  → 读 wav（本项目所有音频均为 wav）
- torchaudio.save(path, tensor, sr)      → 写 wav
- torchaudio.functional.resample(w, ...) → 重采样

本包放在项目根目录（app/main.py 已把项目根加入 sys.path，优先于
site-packages），用 soundfile + scipy.signal.resample_poly 等价实现，
质量与 torchaudio 默认参数相当（Kaiser 窗多相滤波）。

仅覆盖上述 API；若未来安装真正的 torchaudio，删除本目录即可。
"""

from . import functional  # noqa: F401  （供 `torchaudio.functional.xxx` 访问）

__version__ = "0.0.0+soundfile-stub"


def load(filepath, frame_offset=0, num_frames=-1, normalize=True, channels_first=True):
    """读取音频文件，返回 (Tensor[C, N] 或 [N, C], sample_rate)。"""
    import soundfile as sf
    import torch

    with sf.SoundFile(str(filepath)) as f:
        sr = int(f.samplerate)
        if frame_offset or num_frames >= 0:
            f.seek(frame_offset)
            frames = f.read(num_frames if num_frames >= 0 else -1, dtype="float32",
                            always_2d=True)
        else:
            frames = f.read(dtype="float32", always_2d=True)

    data = torch.from_numpy(frames)  # (N, C) float32
    if channels_first:
        data = data.t()  # (C, N)
    return data, sr


def save(filepath, src, sample_rate, channels_first=True):
    """写出音频（PCM16 wav / 按扩展名）。"""
    import soundfile as sf
    import torch

    data = src.detach().cpu() if isinstance(src, torch.Tensor) else src
    if channels_first and data.ndim == 2:
        data = data.t()  # → (N, C)
    sf.write(str(filepath), data, int(sample_rate))


def info(filepath):
    """返回音频元信息（sample_rate / num_frames / num_channels）。"""
    import soundfile as sf

    meta = sf.info(str(filepath))

    class _Info:
        pass

    i = _Info()
    i.sample_rate = int(meta.samplerate)
    i.num_frames = int(meta.frames)
    i.num_channels = int(meta.channels)
    i.duration = float(meta.duration)
    return i
