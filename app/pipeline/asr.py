# -*- coding: utf-8 -*-
"""ASR 转写模块：MOSS-Transcribe-Diarize（时间戳 + 说话人分离）。

MOSS remote code 按 transformers 5.x 编写，与 TTS 所需的 4.x 冲突，故
模型加载与推理运行在 ML worker 子进程中（app/pipeline/ml_worker.py，
见 ml_client.py 模块说明）。本模块保留 ASRModule 的 load / transcribe /
unload 接口（runner.py 无需感知子进程化）。

切块与合并策略（worker 内实现）：
- 超过 chunk_minutes 分钟的音频切块后逐块推理：优先 silero-vad 在静音
  间隙处切（C 点切块，句子不跨块）；VAD 不可用时回退固定步进切块
  （块间重叠 10 秒，重叠区去重拼接）；
- 各块说话人标签按块顺序全局偏移重编号；生成 token 预算按块时长
  动态估算。
"""

from __future__ import annotations

import logging
from typing import Optional

from app.core.segment import Segment
from app.pipeline.base import ProgressCallback
from app.pipeline.ml_client import MLEngine

logger = logging.getLogger(__name__)

# 转写输出 token 密度（token/秒音频）：中文多说话人实测约 8.9，留余量取 12。
# 单块生成预算 = 块时长 × 该密度 + 256，上限 MAX_NEW_TOKENS_CAP。
TOKENS_PER_SECOND = 12
TOKENS_BASE = 256
MAX_NEW_TOKENS_CAP = 16384


class ASRModule:
    """ASR 模块：加载 / 转写 / 释放 MOSS-Transcribe-Diarize 模型。

    worker 子进程在首次 load() 时启动并保持常驻（transcribe 可多次
    调用），unload() 结束进程释放显存。
    """

    def __init__(self) -> None:
        self._engine: MLEngine | None = None

    @property
    def loaded(self) -> bool:
        return self._engine is not None and self._engine.loaded

    def load(self, model_dir: str, device: str = "cpu",
             progress: Optional[ProgressCallback] = None) -> None:
        """启动 worker 并加载模型（bf16 + sdpa + trust_remote_code）。"""
        self.unload()
        engine = MLEngine()
        self._engine = engine  # 先挂引用：加载失败时 unload() 能回收子进程
        engine.start(progress)
        engine.asr_load(model_dir, device, progress)

    def transcribe(
        self,
        wav_path: str,
        chunk_minutes: int = 8,
        progress: Optional[ProgressCallback] = None,
    ) -> list[Segment]:
        """转写 wav 文件，返回按时间排序的 Segment 列表。"""
        if not self.loaded:
            raise RuntimeError("ASR 模型未加载，请先调用 load()")
        assert self._engine is not None
        raw = self._engine.asr_transcribe(wav_path, chunk_minutes, progress)
        segments = [
            Segment(spk=item["spk"], start=item["start"],
                    end=item["end"], text=item["text"])
            for item in raw
        ]
        segments.sort(key=lambda s: s.start)
        return segments

    def unload(self) -> None:
        """释放模型并结束 worker 子进程。"""
        engine = self._engine
        self._engine = None
        if engine is None:
            return
        try:
            if engine.loaded:
                engine.asr_unload()
        except Exception:  # noqa: BLE001 - 释放路径尽力而为，不掩盖主异常
            logger.warning("ASR 模型释放失败（忽略）", exc_info=True)
        engine.shutdown()
