# -*- coding: utf-8 -*-
"""ASR 转写模块：MOSS-Transcribe-Diarize（时间戳 + 说话人分离）。"""

from __future__ import annotations

import difflib
import gc
import logging
import re
import subprocess
import tempfile
import types
from pathlib import Path
from typing import Optional

import soundfile as sf
import torch
import transformers
from transformers import AutoModelForCausalLM, AutoProcessor
from transformers.audio_utils import load_audio

from app.core.ffmpeg import get_ffmpeg_path
from app.core.segment import Segment
from app.pipeline.base import ProgressCallback, report_progress, weights_loading_progress

logger = logging.getLogger(__name__)

# transformers 主版本号：4.x 用 torch_dtype 参数，5.x 用 dtype 参数
_TF_MAJOR = int(transformers.__version__.split(".")[0])

_COMPAT_DONE = False


def _ensure_transformers_compat() -> None:
    """transformers 4.52/5.x 兼容垫片（幂等）。

    MOSS-Transcribe-Diarize 的 remote code 按 transformers 5.x 编写，
    当前 venv 为 4.52.1，需要三处兼容：

    1. ``torch_compilable_check``：remote code 从 transformers.utils 导入的
       运行时校验函数，4.52 中不存在，注入等价实现；
    2. ``extra_special_tokens`` 列表形式：tokenizer_config.json 由 5.x 保存，
       该字段为列表而 4.52 期望 dict；audio token（id 151669~151671）已在
       tokenizer.json 中注册，列表可安全忽略；
    3. ``dtype`` 参数名：5.x 用 ``dtype``（且传 torch.dtype 对象会触发
       config repr 的 JSON 序列化 bug，需用字符串），4.x 用 ``torch_dtype``。
    """
    global _COMPAT_DONE
    if _COMPAT_DONE:
        return

    import transformers.utils as tf_utils

    if not hasattr(tf_utils, "torch_compilable_check"):
        def _torch_compilable_check(condition, message=""):
            if not condition:
                raise ValueError(message or "torch_compilable_check failed")

        tf_utils.torch_compilable_check = _torch_compilable_check

    from transformers.tokenization_utils_base import PreTrainedTokenizerBase

    _orig_set_tokens = PreTrainedTokenizerBase._set_model_specific_special_tokens

    def _set_model_specific_special_tokens_compat(self, special_tokens):
        if isinstance(special_tokens, (list, tuple)):
            special_tokens = {}
        return _orig_set_tokens(self, special_tokens)

    PreTrainedTokenizerBase._set_model_specific_special_tokens = (
        _set_model_specific_special_tokens_compat
    )
    _COMPAT_DONE = True


_ensure_transformers_compat()

# 模型卡默认提示词（优化用于时间戳 + 说话人分离转写，输出 [start][S01] text[end]）
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)

# 解析模型输出行：[0.00][S01] hello world[2.30]
SEGMENT_RE = re.compile(r"\[(\d+(?:\.\d+)?)\]\[(S\d+)\](.*?)\[(\d+(?:\.\d+)?)\]", re.S)

# 块间重叠（秒）：相邻块重叠区用于去重拼接
CHUNK_OVERLAP = 10.0


def _normalize_text(text: str) -> str:
    """文本相似度用的归一化：去空白与标点、转小写。"""
    return re.sub(r"[\W_]+", "", text.lower(), flags=re.UNICODE)


def _texts_similar(a: str, b: str) -> bool:
    """判断两段文本是否相似（重叠区去重用）。"""
    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.5


def _time_overlaps(a: Segment, b: Segment, eps: float = 0.2) -> bool:
    """两段时间戳是否重叠。"""
    return a.start < b.end - eps and b.start < a.end - eps


class ASRModule:
    """ASR 模块：加载 / 转写 / 释放 MOSS-Transcribe-Diarize 模型。"""

    def __init__(self) -> None:
        self._model = None
        self._processor = None
        self._device = "cpu"

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def load(self, model_dir: str, device: str = "cpu", progress=None) -> None:
        """按 POC 验证方式加载模型（bf16 + sdpa + trust_remote_code）。"""
        self.unload()
        self._device = device
        # 4.x 用 torch_dtype；5.x 用 dtype（字符串形式规避 config repr 的
        # torch.dtype 对象 JSON 序列化 bug），两者均按 bf16 加载权重
        dtype_kwargs = (
            {"dtype": "bfloat16"} if _TF_MAJOR >= 5 else {"torch_dtype": torch.bfloat16}
        )
        report_progress(progress, 0.02, "加载转写模型权重")
        with weights_loading_progress(progress, 0.02, 0.08, "加载转写模型权重"):
            self._model = AutoModelForCausalLM.from_pretrained(
                model_dir,
                trust_remote_code=True,
                attn_implementation="sdpa",
                **dtype_kwargs,
            ).eval()
            if device != "cpu":
                self._model.to(device)
            self._processor = AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)
        # 4.52 不会读取独立的 chat_template.jinja（5.x 约定），手动补挂
        tokenizer = getattr(self._processor, "tokenizer", None)
        if tokenizer is not None and not getattr(tokenizer, "chat_template", None):
            tmpl = Path(model_dir) / "chat_template.jinja"
            if tmpl.is_file():
                tokenizer.chat_template = tmpl.read_text(encoding="utf-8")

        # transformers 4.52 的生成循环调用 prepare_inputs_for_generation 时
        # 不传 is_first_iteration（remote code 按 5.x 约定编写，5.x 才传），
        # 导致 remote code 仅在首步注入的 input_features 音频特征永远进
        # 不了模型、输出乱码。这里给模型实例打运行时补丁：当
        # past_key_values 为空（即生成首步）时强制 is_first_iteration=True，
        # 保证首步注入音频特征；后续步 KV 缓存已含提示词，无需再注入。
        orig_pifg = type(self._model).prepare_inputs_for_generation

        def _pifg_compat(model_self, input_ids, *args, **kwargs):
            past = kwargs.get("past_key_values")
            if past is None or (
                hasattr(past, "get_seq_length") and past.get_seq_length() == 0
            ):
                kwargs["is_first_iteration"] = True
            return orig_pifg(model_self, input_ids, *args, **kwargs)

        self._model.prepare_inputs_for_generation = types.MethodType(
            _pifg_compat, self._model
        )

    def unload(self) -> None:
        """释放模型与处理器。"""
        self._model = None
        self._processor = None
        gc.collect()
        if self._device != "cpu" and torch.cuda.is_available():
            torch.cuda.empty_cache()

    # ------------------------------------------------------------------
    # 单块推理
    # ------------------------------------------------------------------

    def _transcribe_chunk(self, wav_path: str) -> list[dict]:
        """对单个音频文件推理，返回解析后的分段（时间相对该文件起点）。"""
        processor = self._processor
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "audio", "audio": str(wav_path)},
                    {"type": "text", "text": DEFAULT_PROMPT},
                ],
            }
        ]
        text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        audio = load_audio(str(wav_path), sampling_rate=processor.feature_extractor.sampling_rate)
        inputs = processor(text=text, audio=[audio], max_length=131072, return_tensors="pt")
        if self._device != "cpu":
            inputs = {k: (v.to(self._device) if hasattr(v, "to") else v) for k, v in inputs.items()}
        prompt_len = int(inputs["attention_mask"][0].sum())

        with torch.inference_mode():
            outputs = self._model.generate(
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                input_features=inputs["input_features"],
                audio_feature_lengths=inputs["audio_feature_lengths"],
                audio_chunk_mapping=inputs["audio_chunk_mapping"],
                max_new_tokens=2048,
                do_sample=False,
            )
        gen_ids = outputs[0][prompt_len:]
        result = processor.tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
        return [
            {"start": float(a), "spk": sp, "text": body.strip(), "end": float(b)}
            for a, sp, body, b in SEGMENT_RE.findall(result)
        ]

    # ------------------------------------------------------------------
    # 切块与合并
    # ------------------------------------------------------------------

    def _cut_chunk(self, wav_path: str, start: float, length: float, out_path: Path) -> None:
        """用 ffmpeg 从 wav 中切块（16kHz 单声道，降低临时文件体积）。"""
        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("未找到内置 ffmpeg（libs/windows/ffmpeg.exe）")
        cmd = [
            ffmpeg, "-y", "-nostdin",
            "-ss", f"{start:.3f}",
            "-i", wav_path,
            "-t", f"{length:.3f}",
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
            raise RuntimeError(f"切块失败: [{start:.1f}s, +{length:.1f}s] -> {out_path}")

    def _merge_chunk(self, merged: list[Segment], chunk_segs: list[Segment]) -> None:
        """把一块的分段合并进全局列表。

        - 重叠区去重：与已合并分段时间重叠且文本相似的，保留后块的版本；
        - 说话人标签统一：按块顺序对每块的 spk 做全局偏移重编号，保证
          全局不重号（同一编号不会指向不同的人）。

        TODO: 当前简化方案下，跨块的同一说话人会被编成不同编号
        （单块 <= chunk_minutes 内标签一致）。后续可用声纹聚类
        （如说话人向量 + 层次聚类）把跨块同人合并为同一编号。
        """
        for seg in chunk_segs:
            replaced = False
            for j, existing in enumerate(merged):
                if _time_overlaps(existing, seg) and _texts_similar(existing.text, seg.text):
                    merged[j] = seg  # 保留后块的版本
                    replaced = True
                    break
            if not replaced:
                merged.append(seg)

    def transcribe(
        self,
        wav_path: str,
        chunk_minutes: int = 8,
        progress: Optional[ProgressCallback] = None,
    ) -> list[Segment]:
        """转写 wav 文件，返回按时间排序的 Segment 列表。

        超过 chunk_minutes 分钟的音频用 ffmpeg 切块（块间重叠 10 秒，
        临时文件用完即删），逐块推理后按重叠区去重拼接；各块说话人
        标签按块顺序全局偏移重编号。
        """
        if not self.loaded:
            raise RuntimeError("ASR 模型未加载，请先调用 load()")

        info = sf.info(wav_path)
        duration = info.frames / info.samplerate
        chunk_seconds = chunk_minutes * 60.0

        # 单块即可容纳：直接推理
        if duration <= chunk_seconds + CHUNK_OVERLAP:
            report_progress(progress, 0.1, f"转写中（{duration:.1f} 秒音频）")
            raw = self._transcribe_chunk(wav_path)
            report_progress(progress, 0.95, "转写完成，解析分段")
            segments = [
                Segment(spk=item["spk"], start=item["start"], end=item["end"], text=item["text"])
                for item in raw
            ]
            segments.sort(key=lambda s: s.start)
            report_progress(progress, 1.0, f"转写完成，共 {len(segments)} 段")
            return segments

        # 长音频：切块处理（步长 = 块长 - 重叠）
        stride = chunk_seconds - CHUNK_OVERLAP
        starts: list[float] = []
        start = 0.0
        while start < duration:
            starts.append(start)
            start += stride
        total = len(starts)

        merged: list[Segment] = []
        spk_offset = 0  # 已使用的全局说话人编号数
        with tempfile.TemporaryDirectory(prefix="asr_chunks_") as tmp:
            tmp_dir = Path(tmp)
            for i, chunk_start in enumerate(starts):
                chunk_len = min(chunk_seconds, duration - chunk_start)
                chunk_file = tmp_dir / f"chunk_{i:04d}.wav"
                self._cut_chunk(wav_path, chunk_start, chunk_len, chunk_file)
                raw = self._transcribe_chunk(str(chunk_file))
                chunk_file.unlink(missing_ok=True)

                # 块内局部标签 -> 全局偏移标签
                local_max = 0
                chunk_segs: list[Segment] = []
                for item in raw:
                    m = re.fullmatch(r"S(\d+)", item["spk"])
                    local_idx = int(m.group(1)) if m else 1
                    local_max = max(local_max, local_idx)
                    global_spk = f"S{spk_offset + local_idx:02d}"
                    chunk_segs.append(
                        Segment(
                            spk=global_spk,
                            start=chunk_start + item["start"],
                            end=chunk_start + item["end"],
                            text=item["text"],
                        )
                    )
                spk_offset += local_max

                self._merge_chunk(merged, chunk_segs)
                report_progress(
                    progress,
                    (i + 1) / total,
                    f"转写块 {i + 1}/{total}（{chunk_start:.0f}~{chunk_start + chunk_len:.0f} 秒）",
                )

        merged.sort(key=lambda s: s.start)
        report_progress(progress, 1.0, f"转写完成，共 {len(merged)} 段")
        return merged
