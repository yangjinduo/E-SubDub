# -*- coding: utf-8 -*-
"""人声分离：demucs htdemucs two-stems（vocals / no_vocals）。

推理运行在 .venv-ml 的 worker 子进程中（app/pipeline/ml_worker.py，
见 ml_client.py 模块说明）；本模块只保留对 runner 的稳定函数接口：
separate_vocals(full_audio_wav, work_dir, device, progress) 启动一次
worker 完成分离后立即结束（demucs 模型小，无跨阶段常驻价值）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from app.pipeline.base import ProgressCallback
from app.pipeline.ml_client import MLEngine

MODEL_NAME = "htdemucs"


def separate_vocals(
    full_audio_wav: str,
    work_dir: Path,
    device: str = "cpu",
    progress: Optional[ProgressCallback] = None,
) -> tuple[str, str]:
    """把 full_audio_wav 分离为人声与伴奏，返回 (vocals_path, no_vocals_path)。

    - 模型 htdemucs，权重从资源管理器部署的 models/demucs/htdemucs/
      955717e8.safetensors（HF 官方包，内嵌架构元数据）本地加载，
      缺失时报错，不再联网下载；
    - 输出 work_dir/vocals.wav 与 work_dir/no_vocals.wav（44.1kHz 立体声 PCM16）；
    - 长音频由 demucs 自带 chunk 机制自动分块（split=True + 默认 segment，
      块间 overlap 0.25 加权拼接）；
    - device 支持 "cuda"/"mps"/"cpu"，由 worker 内 torch.device 解析。
    """
    from app.resources.model import get_resource_manager

    model_dir = get_resource_manager().get_resource_path("demucs")
    if not model_dir:
        raise RuntimeError(
            "人声分离模型未就绪（请在 设置 → 资源管理 中下载人声分离模型）"
        )
    engine = MLEngine()
    try:
        engine.start(progress)
        return engine.separate(full_audio_wav, str(work_dir), model_dir, device, progress)
    finally:
        engine.shutdown()
