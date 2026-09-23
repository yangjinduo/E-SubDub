# -*- coding: utf-8 -*-
"""声音克隆 TTS：MOSS-TTS-Local-1.7B 进程内推理引擎。

与主流水线共用 transformers 5.17（trust_remote_code），无独立 venv / 子进程：
- 零样本克隆：仅需参考音频（无需转写文本），build_user_message(reference=[wav])；
- 时长控制：原生 token 级（1s ≈ 12.5 tokens），消息内 tokens 提示 + generate
  max_new_tokens 上限双重约束；偏差仍超限时用 time-stretch 兜底对齐；
- audio tokenizer 为独立仓库（openmoss/MOSS-Audio-Tokenizer，约 7.1GB），
  processor 构造时通过 codec_path 指定本地目录。
"""

from __future__ import annotations

import gc
import logging
from pathlib import Path
from typing import Optional

import soundfile as sf
import torch
import transformers

from app.pipeline.base import ProgressCallback, report_progress, weights_loading_progress
from app.pipeline.mix import stretch_to_duration

logger = logging.getLogger(__name__)

# transformers 主版本：4.x 用 torch_dtype 参数，5.x 用 dtype 参数
_TF_MAJOR = int(transformers.__version__.split(".")[0])

# 音频 token 速率：1 秒 ≈ 12.5 tokens（官方 README）
TOKENS_PER_SECOND = 12.5
# 伸缩比超过该区间（需 >1.3x 压缩 / >1.43x 拉长）时标记 needs_review
STRETCH_REVIEW_HIGH = 1.3   # 生成时长 / 目标时长 上限
STRETCH_REVIEW_LOW = 0.7    # 下限
# 生成时长与目标偏差在该比例内直接使用，不做 time-stretch
STRETCH_TOLERANCE = 0.10
# max_new_tokens 相对目标 tokens 的富余（防止轻微超出被截断）
_TOKEN_MARGIN = 64

# 官方推荐解码参数（MOSS-TTSLocal-1.7B）
_GEN_KWARGS = dict(
    audio_temperature=1.0,
    audio_top_p=0.95,
    audio_top_k=50,
    audio_repetition_penalty=1.1,
)


class TTSLoadMemoryError(RuntimeError):
    """内存/页面文件不足导致 TTS 加载失败（换 CPU 重试只会更糟，直接失败）。"""


def _apply_tf5_compat_patches(model) -> None:
    """transformers 5.x 运行时兼容补丁（模型按 4.x 行为编写，不改模型文件）。

    1. 5.x 的 DynamicCache 初始化读取主 config 的 ``num_hidden_layers`` /
       ``layer_types``，而 MOSS-TTS 把它们放在 ``language_config`` 子字典内，
       缺失时 ``generate()`` 抛 AttributeError → 从子字典补齐；
    2. 4.x 的 KV cache 由模型 forward 内部管理（attention 层断言
       ``past_key_value is None``），5.x 改为 generate 预创建 DynamicCache
       并传入 → 跳过预创建，保持 4.x 的 cache 路径；
    3. 5.x 删除了 ``GenerationMixin._get_initial_cache_position``，而 remote
       code 的 ``_sample`` 启动时仍按 (cur_len, device, model_kwargs) 签名
       调用（缺失时 AttributeError，即本次崩溃点）→ 按 4.x 语义重建：
       cache_position = arange(past_len, cur_len)；
    4. 4.x ``prepare_inputs_for_generation`` 会依据 past_key_values 把
       input_ids 裁剪为未处理的 token，5.x 改由内置 _sample 传
       next_sequence_length 控制切片；remote 的 _sample 每步传全量
       input_ids → 已有 KV cache 时手动裁剪到最后一个 token，否则整段
       序列会重复写入 cache 且位置编号错乱（生成乱码音频）；
    5. remote code 以 4.x 签名调用 ``create_causal_mask``（
       ``input_embeds`` + ``cache_position``），5.x 参数改名为
       ``inputs_embeds`` 且已移除 ``cache_position`` → 在 remote
       模块命名空间内包一层参数翻译（local_transformer.forward 内部
       按模块名直接调用，无法在实例上打补丁）。
    """
    import sys
    import types

    cfg = model.config
    if getattr(cfg, "num_hidden_layers", None) is None:
        lc = getattr(cfg, "language_config", None)
        if lc is not None and getattr(lc, "num_hidden_layers", None) is not None:
            cfg.num_hidden_layers = lc.num_hidden_layers
            if getattr(cfg, "layer_types", None) is None and getattr(lc, "layer_types", None):
                cfg.layer_types = list(lc.layer_types)
    model._prepare_cache_for_generation = lambda *args, **kwargs: None

    if not hasattr(model, "_get_initial_cache_position"):
        def _get_initial_cache_position(_self, cur_len, device, model_kwargs):
            past_length = 0
            pkv = model_kwargs.get("past_key_values")
            if pkv is not None:
                if hasattr(pkv, "get_seq_length") and pkv.get_seq_length() is not None:
                    past_length = int(pkv.get_seq_length())
                elif isinstance(pkv, (list, tuple)) and pkv and pkv[0] is not None:
                    past_length = int(pkv[0][0].shape[2])
            model_kwargs["cache_position"] = torch.arange(
                past_length, cur_len, device=device,
            )
            return model_kwargs

        model._get_initial_cache_position = types.MethodType(
            _get_initial_cache_position, model,
        )

    orig_prepare = type(model).prepare_inputs_for_generation

    def _prepare_inputs_compat(model_self, input_ids, *args, **kwargs):
        past = kwargs.get("past_key_values")
        if (
            past is not None
            and getattr(past, "get_seq_length", None) is not None
            and past.get_seq_length() > 0
        ):
            input_ids = input_ids[:, -1:]
        return orig_prepare(model_self, input_ids, *args, **kwargs)

    model.prepare_inputs_for_generation = types.MethodType(
        _prepare_inputs_compat, model,
    )

    # create_causal_mask 参数翻译：input_embeds → inputs_embeds，丢弃 cache_position
    local_tf = getattr(model, "local_transformer", None)
    if local_tf is not None:
        remote_mod = sys.modules.get(type(local_tf).__module__)
        if (
            remote_mod is not None
            and hasattr(remote_mod, "create_causal_mask")
            and not getattr(remote_mod, "_subdub_ccm_patched", False)
        ):
            orig_ccm = remote_mod.create_causal_mask

            def _create_causal_mask_compat(*args, **kwargs):
                if "input_embeds" in kwargs:
                    kwargs["inputs_embeds"] = kwargs.pop("input_embeds")
                kwargs.pop("cache_position", None)
                return orig_ccm(*args, **kwargs)

            remote_mod.create_causal_mask = _create_causal_mask_compat
            remote_mod._subdub_ccm_patched = True


class TTSEngine:
    """MOSS-TTS-Local-1.7B 推理引擎：加载 / 合成 / 释放。"""

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._device = "cpu"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(
        self,
        model_dir: str,
        codec_dir: str,
        device: str = "cpu",
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        """加载 TTS 模型与 audio tokenizer（顺序与 dtype 针对低内存机器优化）。

        codec_dir 为 MOSS-Audio-Tokenizer 本地目录（processor 必需）。

        内存策略（16GB 内存 + 6GB 显存机器实测约束，os error 1455 复盘）：
        - 先加载主模型（bf16 约 3.1GB）并移入计算设备、释放内存，
          再加载 audio tokenizer（fp32 约 6.6GB）。若先加载 codec，
          16GB 内存机器加载主模型时 mmap 提交失败（页面文件太小 1455）；
        - CPU 路径主模型同样用 bf16：fp32 主模型(6.1GB)+codec(6.6GB)
          合计 12.7GB 会压爆 16GB 内存，bf16 后合计约 9.7GB；
        - codec 仅在 GPU 剩余显存 > 1.15 倍体积时上 GPU，移入失败留 CPU
          ——processor 按 codec 自身设备独立编解码（_get_audio_tokenizer_device），
          跨设备安全；大显存机器 codec 自动上 GPU 提速。
        """
        self.unload()
        self._device = device
        # mps 的 bf16 路径未验证，保持 fp32；cuda/cpu 用 bf16（精度足够且内存减半）
        dtype = torch.float32 if device == "mps" else torch.bfloat16
        dtype_kwargs = {"dtype": dtype} if _TF_MAJOR >= 5 else {"torch_dtype": dtype}
        attn = "sdpa" if device in ("cuda", "mps") else "eager"

        from transformers import AutoModel, AutoProcessor

        def _is_memory_error(exc: BaseException) -> bool:
            # Windows 页面文件不足：OSError errno/winerror 1455；
            # Rust safetensors 抛的 OSError 里 errno 只在消息文本 "(os error 1455)" 中
            return (
                isinstance(exc, MemoryError)
                or getattr(exc, "winerror", None) == 1455
                or getattr(exc, "errno", None) == 1455
                or "1455" in str(exc)
            )

        # 1. 主模型：先加载并移入计算设备，尽早释放内存（GPU 3.1GB / CPU 同为 3.1GB）
        report_progress(progress, 0.05, f"加载声音克隆模型 MOSS-TTS（{device}）")
        try:
            with weights_loading_progress(progress, 0.05, 0.45, "加载声音克隆模型权重"):
                self._model = AutoModel.from_pretrained(
                    model_dir,
                    trust_remote_code=True,
                    attn_implementation=attn,
                    **dtype_kwargs,
                ).to(device)
        except (MemoryError, OSError) as exc:
            if _is_memory_error(exc):
                raise TTSLoadMemoryError(
                    "内存不足：加载 TTS 主模型失败（约需 4GB 可用内存）。"
                    "请关闭其他程序后重试；若仍失败，请增大虚拟内存"
                    "（页面文件）或清理系统盘空间。"
                ) from exc
            raise
        self._model.eval()
        _apply_tf5_compat_patches(self._model)

        # 2. 处理器 + audio tokenizer（fp32 约 6.6GB，本阶段为内存峰值）
        report_progress(progress, 0.5, f"加载 TTS 处理器（含 audio tokenizer）: {Path(codec_dir).name}")
        try:
            with weights_loading_progress(progress, 0.5, 0.95, "加载音频编解码器权重"):
                self._processor = AutoProcessor.from_pretrained(
                    model_dir,
                    trust_remote_code=True,
                    codec_path=codec_dir,
                )
        except (MemoryError, OSError) as exc:
            if _is_memory_error(exc):
                raise TTSLoadMemoryError(
                    "内存不足：加载 audio tokenizer（约 6.6GB）失败。"
                    "请关闭其他程序后重试；若仍失败，请增大虚拟内存"
                    "（页面文件）或清理系统盘空间。"
                ) from exc
            raise

        # 3. codec 安置：GPU 剩余显存足够才上 GPU；移入失败降级留 CPU
        tokenizer = self._processor.audio_tokenizer
        codec_on_gpu = False
        need_gb = free_gb = 0.0
        try:
            if device == "cuda":
                need = sum(p.numel() * p.element_size() for p in tokenizer.parameters())
                free, _total = torch.cuda.mem_get_info()
                need_gb, free_gb = need / 2**30, free / 2**30
                if free > need * 1.15:
                    self._processor.audio_tokenizer = tokenizer.to(device)
                    codec_on_gpu = True
                    logger.info("audio tokenizer 已移入 GPU（剩余显存 %.1fGB）", free_gb)
            elif device == "mps":
                self._processor.audio_tokenizer = tokenizer.to(device)
                codec_on_gpu = True
        except RuntimeError as exc:
            logger.warning("audio tokenizer 移入 %s 失败，保留在 CPU（跨设备安全）: %r", device, exc)
        if device != "cpu":
            # 界面可见 codec 安置：codec 留 CPU 时生成阶段 CPU 占用会明显偏高
            report_progress(
                progress, 0.97,
                "音频编解码器已上 GPU" if codec_on_gpu
                else f"音频编解码器留在 CPU（剩余显存 {free_gb:.1f}GB，需约 {need_gb:.1f}GB）",
            )

        report_progress(progress, 1.0, "TTS 模型加载完成")

    def unload(self) -> None:
        """释放模型与处理器。"""
        self._model = None
        self._processor = None
        gc.collect()
        if torch.cuda.is_available():
            torch.cuda.empty_cache()

    # ------------------------------------------------------------------ 合成

    def synthesize(
        self,
        text: str,
        ref_wav: str,
        target_duration: float,
        out_path: str,
    ) -> tuple[float, float]:
        """克隆参考音色合成一句语音，返回 (实际时长, 伸缩比)。

        - token 级时长控制：目标 tokens = target_duration × 12.5，写入消息提示
          并作为 max_new_tokens 上限（含少量富余）；
        - 生成后偏差仍在 STRETCH_TOLERANCE 外时用 time-stretch 对齐到目标时长；
        - stretch_ratio = 原始生成时长 / 目标时长，超出 [0.7, 1.3] 由调用方
          标记 needs_review。
        """
        if not self.loaded:
            raise RuntimeError("TTS 模型未加载，请先调用 load()")
        if not Path(ref_wav).is_file():
            raise FileNotFoundError(f"参考音频不存在: {ref_wav}")

        target_tokens = max(int(round(target_duration * TOKENS_PER_SECOND)), 8)
        conv = [[
            self._processor.build_user_message(
                text=text, reference=[str(ref_wav)], tokens=target_tokens,
            )
        ]]
        batch = self._processor(conv, mode="generation")
        input_ids = batch["input_ids"].to(self._device)
        attention_mask = batch["attention_mask"].to(self._device)

        with torch.inference_mode():
            outputs = self._model.generate(
                input_ids=input_ids,
                attention_mask=attention_mask,
                max_new_tokens=target_tokens + _TOKEN_MARGIN,
                **_GEN_KWARGS,
            )

        message = self._processor.decode(outputs)[0]
        audio = message.audio_codes_list[0]
        sr = int(self._processor.model_config.sampling_rate)
        wav = audio.detach().cpu().to(torch.float32).numpy()

        # 去掉首尾静音，避免拉伸判断与混音排布被静音干扰
        trimmed = _trim_silence(wav)

        Path(out_path).parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), trimmed, sr, subtype="PCM_16")
        raw_duration = len(trimmed) / sr

        ratio = raw_duration / target_duration if target_duration > 0 else 1.0
        if abs(raw_duration - target_duration) / max(target_duration, 1e-6) > STRETCH_TOLERANCE:
            stretched = stretch_to_duration(str(out_path), target_duration, str(out_path))
            logger.info(
                "TTS 时长对齐: 原始 %.2fs → 目标 %.2fs（伸缩比 %.2f，拉伸后 %.2fs）",
                raw_duration, target_duration, ratio, stretched,
            )
        return raw_duration, ratio


def _trim_silence(wav, threshold_db: float = -45.0) -> "object":
    """去掉首尾近静音（按帧 RMS 阈值），全静音时原样返回。

    使用 30ms 帧长扫描，从首/尾找到第一个超阈帧，向前/向后多保留 60ms 余量，
    保证起音自然。
    """
    import numpy as np

    if wav is None or len(wav) == 0:
        return wav
    data = np.asarray(wav, dtype=np.float32)
    frame = max(1, int(0.03 * 24000))  # 帧长按采样率无关的近似值扫描
    n = len(data) // frame
    if n < 3:
        return data
    frames = data[: n * frame].reshape(n, frame)
    rms = np.sqrt((frames ** 2).mean(axis=1))
    thr = 10.0 ** (threshold_db / 20.0)
    nonzero = np.nonzero(rms > thr)[0]
    if len(nonzero) == 0:
        return data
    pad_frames = 2  # 约 60ms 余量
    start = max(0, (nonzero[0] - pad_frames) * frame)
    end = min(len(data), (nonzero[-1] + 1 + pad_frames) * frame)
    return data[start:end]
