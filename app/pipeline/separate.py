# -*- coding: utf-8 -*-
"""人声分离：demucs htdemucs two-stems（vocals / no_vocals）Python API 封装。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import numpy as np
import soundfile as sf
import torch as th

from app.pipeline.base import ProgressCallback, report_progress

from demucs.apply import apply_model
from demucs.audio import convert_audio
from demucs.pretrained import REMOTE_ROOT, _parse_remote_files, get_model
from demucs.repo import AnyModelRepo, BagOnlyRepo, RemoteRepo

MODEL_NAME = "htdemucs"

logger = logging.getLogger(__name__)


def _load_demucs_model(name: str = MODEL_NAME):
    """加载 demucs 预训练模型，返回 eval 模式模型。

    优先走 demucs 官方 legacy 仓库路径（直接命中 torch hub 缓存，
    无网络请求；未缓存时从官方 AWS 源下载）；失败再退回标准
    get_model()（先试 HuggingFace hub，再试 legacy）。
    这样在无法直连 huggingface.co 的环境下不会浪费约 25 秒重试。
    """
    try:
        models = _parse_remote_files(REMOTE_ROOT / "files.txt")
        model_repo = RemoteRepo(models)
        bag_repo = BagOnlyRepo(REMOTE_ROOT, model_repo)
        repo = AnyModelRepo(model_repo, bag_repo)
        model = repo.get_model(name)
        model.eval()
        return model
    except Exception as exc:
        logger.warning("legacy 仓库加载 %s 失败（%r），改用标准 get_model", name, exc)
        return get_model(name)


def separate_vocals(
    full_audio_wav: str,
    work_dir: Path,
    device: str = "cpu",
    progress: Optional[ProgressCallback] = None,
) -> tuple[str, str]:
    """把 full_audio_wav 分离为人声与伴奏，返回 (vocals_path, no_vocals_path)。

    - 模型 htdemucs（torch hub 缓存），two-stems vocals 模式；
    - 输出 work_dir/vocals.wav 与 work_dir/no_vocals.wav（44.1kHz 立体声 PCM16）；
    - 长音频由 demucs 自带 chunk 机制自动分块（split=True + 默认 segment，
      块间 overlap 0.25 加权拼接，参考 demucs.separate CLI 默认参数）；
    - device 预留 "cuda"/"mps"，CPU 上显式使用 torch.device("cpu")。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    vocals_path = work_dir / "vocals.wav"
    no_vocals_path = work_dir / "no_vocals.wav"

    report_progress(progress, 0.02, f"加载人声分离模型 {MODEL_NAME}")
    model = _load_demucs_model(MODEL_NAME)
    target_device = th.device(device)

    # 读取音频并适配模型采样率/声道（参考 demucs.api.Separator 写法）
    data, sr = sf.read(full_audio_wav, dtype="float32", always_2d=True)
    wav = th.from_numpy(np.ascontiguousarray(data.T))  # (C, T)
    if sr != model.samplerate or wav.shape[0] != model.audio_channels:
        wav = convert_audio(wav, sr, model.samplerate, model.audio_channels)

    # 整体幅值归一化，提升分离稳定性（与 demucs CLI 一致）
    ref = wav.mean(0)
    mean = ref.mean()
    std = ref.std() + 1e-8

    audio_length = wav.shape[1]

    def _on_chunk(info: dict) -> None:
        """demucs apply_model 回调：按 segment_offset 上报分块进度。"""
        if info.get("state") != "end":
            return
        length = info.get("audio_length") or audio_length
        offset = info.get("segment_offset", 0)
        if length > 0:
            report_progress(
                progress,
                0.1 + 0.85 * min(offset / length, 0.99),
                f"人声分离中（已处理 {offset / model.samplerate:.0f}/"
                f"{length / model.samplerate:.0f} 秒）",
            )

    report_progress(progress, 0.08, "人声分离推理中")
    out = apply_model(
        model,
        ((wav - mean) / std)[None],
        device=target_device,
        shifts=1,
        split=True,
        overlap=0.25,
        callback=_on_chunk,
        callback_arg={"audio_length": audio_length},
    )
    out = out * std + mean

    # two-stems：vocals 单独输出，其余 stem 求和为 no_vocals（CLI 默认 "add" 方式）
    sources = dict(zip(model.sources, out[0]))
    if "vocals" not in sources:
        raise RuntimeError(f"模型 {MODEL_NAME} 不含 vocals 音源: {model.sources}")
    vocals = sources["vocals"]
    others = [sources[name] for name in model.sources if name != "vocals"]
    no_vocals = th.stack(others, dim=0).sum(dim=0)

    report_progress(progress, 0.95, "保存人声/伴奏音频")
    sf.write(str(vocals_path), np.ascontiguousarray(vocals.T.numpy()), model.samplerate, subtype="PCM_16")
    sf.write(str(no_vocals_path), np.ascontiguousarray(no_vocals.T.numpy()), model.samplerate, subtype="PCM_16")
    report_progress(progress, 1.0, "人声分离完成")
    return str(vocals_path), str(no_vocals_path)
