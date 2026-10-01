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
from transformers.generation.streamers import BaseStreamer

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

# 块间重叠（秒）：回退固定步进切块时相邻块重叠区用于去重拼接；
# VAD 切点落在静音处时句子不跨块，无需重叠
CHUNK_OVERLAP = 10.0

# 转写输出 token 密度（token/秒音频）：中文多说话人实测约 8.9，留余量取 12。
# 单块生成预算 = 块时长 × 该密度 + 256，上限 MAX_NEW_TOKENS_CAP。
# 预算不足会导致转写中途被截断（非 EOS 停止），末尾内容静默丢失。
TOKENS_PER_SECOND = 12
TOKENS_BASE = 256
MAX_NEW_TOKENS_CAP = 16384

# C 点切块：候选静音段最短时长（秒）与从目标切点向前找静音的窗口（秒）
MIN_SILENCE = 0.3
CUT_LOOKBACK = 90.0


class _CountingStreamer(BaseStreamer):
    """token 计数流式器：按已生成的新 token 数触发回调（转写进度用）。

    transformers 生成循环先 put 完整提示词，随后每生成一步 put 新 token
    （shape [batch, n]，n 通常为 1），结束时调用 end()。首次 put 的提示词
    不计入生成进度。
    """

    def __init__(self, on_tokens=None) -> None:
        self._on_tokens = on_tokens
        self._count = 0
        self._prompt_done = False  # 首次 put 为提示词，跳过

    def put(self, value) -> None:
        if not self._prompt_done:
            self._prompt_done = True
            return
        n = int(value.shape[-1]) if hasattr(value, "shape") else 1
        self._count += n
        if self._on_tokens is not None:
            self._on_tokens(self._count)

    def end(self) -> None:
        return


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

    def _transcribe_chunk(
        self, wav_path: str, audio_seconds: float, on_tokens=None
    ) -> list[dict]:
        """对单个音频文件推理，返回解析后的分段（时间相对该文件起点）。

        audio_seconds：该块音频时长（秒），用于估算生成 token 预算。
        on_tokens：可选回调 on_tokens(count)，转写过程中每生成若干 token
        调用一次（count 为累计新 token 数），用于进度显示与取消检查。
        """
        processor = self._processor
        # 生成预算按块时长动态估算：预算用尽时 generate 强制停止，
        # 未转完的内容会被静默丢弃（历史 bug：固定 2048 导致长块截断）
        budget = min(
            int(audio_seconds * TOKENS_PER_SECOND) + TOKENS_BASE, MAX_NEW_TOKENS_CAP
        )
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

        streamer = _CountingStreamer(on_tokens) if on_tokens is not None else None
        with torch.inference_mode():
            outputs = self._model.generate(
                input_ids=inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                input_features=inputs["input_features"],
                audio_feature_lengths=inputs["audio_feature_lengths"],
                audio_chunk_mapping=inputs["audio_chunk_mapping"],
                max_new_tokens=budget,
                do_sample=False,
                streamer=streamer,
            )
        gen_ids = outputs[0][prompt_len:]
        # 截断检测：生成数达到预算且末 token 不是 EOS，说明块尾内容可能
        # 未转写完，明确告警而非静默丢失
        if gen_ids.numel() >= budget:
            eos_id = processor.tokenizer.eos_token_id
            eos_ids = eos_id if isinstance(eos_id, (list, tuple)) else [eos_id]
            if int(gen_ids[-1]) not in eos_ids:
                logger.warning(
                    "转写输出达到 max_new_tokens=%d 上限（%.1f 秒音频），"
                    "该块末尾内容可能被截断: %s",
                    budget, audio_seconds, wav_path,
                )
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

    @staticmethod
    def _token_progress(
        progress: Optional[ProgressCallback],
        frac_from: float,
        frac_to: float,
        base_msg: str,
        budget: int,
    ):
        """构造 token 计数 → 进度回调：生成进度映射到 [frac_from, frac_to)。

        budget 为该块的生成 token 预算（按块时长动态估算）。
        消息在 base_msg 基础上按 10% 步进附加百分比（0% 时用 base_msg，
        与起始消息一致以配合 runner 的去重日志，避免刷屏）。
        """

        def on_tokens(count: int) -> None:
            frac = min(count / budget, 1.0)
            pct = min(int(frac * 10), 9) * 10
            report_progress(
                progress,
                frac_from + (frac_to - frac_from) * frac,
                f"{base_msg}{pct}%" if pct else base_msg,
            )

        return on_tokens

    def transcribe(
        self,
        wav_path: str,
        chunk_minutes: int = 8,
        progress: Optional[ProgressCallback] = None,
    ) -> list[Segment]:
        """转写 wav 文件，返回按时间排序的 Segment 列表。

        超过 chunk_minutes 分钟的音频切块后逐块推理：优先用 silero-vad
        在静音间隙处切（C 点切块，句子不跨块）；VAD 不可用时回退固定
        步进切块（块间重叠 10 秒，重叠区去重拼接）。各块说话人标签按
        块顺序全局偏移重编号；生成 token 预算按块时长动态估算。
        """
        if not self.loaded:
            raise RuntimeError("ASR 模型未加载，请先调用 load()")

        info = sf.info(wav_path)
        duration = info.frames / info.samplerate
        chunk_seconds = chunk_minutes * 60.0

        # 单块即可容纳：直接推理
        if duration <= chunk_seconds + CHUNK_OVERLAP:
            base_msg = f"转写中（{duration:.1f} 秒音频）"
            report_progress(progress, 0.1, base_msg)
            budget = min(
                int(duration * TOKENS_PER_SECOND) + TOKENS_BASE, MAX_NEW_TOKENS_CAP
            )
            raw = self._transcribe_chunk(
                wav_path,
                duration,
                self._token_progress(progress, 0.1, 0.9, base_msg, budget),
            )
            report_progress(progress, 0.95, "转写完成，解析分段")
            segments = [
                Segment(spk=item["spk"], start=item["start"], end=item["end"], text=item["text"])
                for item in raw
            ]
            segments.sort(key=lambda s: s.start)
            report_progress(progress, 1.0, f"转写完成，共 {len(segments)} 段")
            return segments

        # 长音频：切块处理。VAD 检测阶段占 [0, VAD_FRAC)，各块占余下区间
        VAD_FRAC = 0.05
        merged: list[Segment] = []
        spk_offset = 0  # 已使用的全局说话人编号数
        with tempfile.TemporaryDirectory(prefix="asr_chunks_") as tmp:
            tmp_dir = Path(tmp)
            # 统一转 16k 单声道中间文件：VAD 与切块共用，避免重复转码
            full16k = tmp_dir / "full_16k.wav"
            self._cut_chunk(wav_path, 0.0, duration, full16k)
            report_progress(progress, VAD_FRAC * 0.5, "检测静音切点（VAD）")
            cut_points = self._find_cut_points(str(full16k), chunk_seconds)
            if cut_points is not None:
                # C 点切块：切点在静音间隙处，句子不跨块，无需重叠
                blocks = [
                    (cut_points[i], cut_points[i + 1] - cut_points[i])
                    for i in range(len(cut_points) - 1)
                ]
                vad_frac = VAD_FRAC
            else:
                # 回退：固定步进切块，块间重叠 CHUNK_OVERLAP 靠去重拼接兜底
                stride = chunk_seconds - CHUNK_OVERLAP
                starts: list[float] = []
                start = 0.0
                while start < duration:
                    starts.append(start)
                    start += stride
                blocks = [
                    (s, min(chunk_seconds, duration - s)) for s in starts
                ]
                vad_frac = 0.0
            report_progress(progress, vad_frac, "切块完成，开始逐块转写")
            total = len(blocks)

            for i, (chunk_start, chunk_len) in enumerate(blocks):
                chunk_file = tmp_dir / f"chunk_{i:04d}.wav"
                self._cut_chunk(str(full16k), chunk_start, chunk_len, chunk_file)
                chunk_msg = f"转写块 {i + 1}/{total}（{chunk_start:.0f}~{chunk_start + chunk_len:.0f} 秒）"
                # 该块的切块/生成/合并各占块内进度的 0~0.1 / 0.1~0.9 / 0.9~1
                frac_a = vad_frac + (1.0 - vad_frac) * (i + 0.1) / total
                frac_b = vad_frac + (1.0 - vad_frac) * (i + 0.9) / total
                budget = min(
                    int(chunk_len * TOKENS_PER_SECOND) + TOKENS_BASE, MAX_NEW_TOKENS_CAP
                )
                raw = self._transcribe_chunk(
                    str(chunk_file),
                    chunk_len,
                    self._token_progress(progress, frac_a, frac_b, chunk_msg, budget),
                )
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
                    vad_frac + (1.0 - vad_frac) * (i + 1) / total,
                    chunk_msg,
                )

        merged.sort(key=lambda s: s.start)
        report_progress(progress, 1.0, f"转写完成，共 {len(merged)} 段")
        return merged

    @staticmethod
    def _find_cut_points(wav16k_path: str, chunk_seconds: float) -> Optional[list[float]]:
        """C 点切块：用 silero-vad 找静音间隙作为切点，句子不跨块。

        从每个目标切点（当前块起点 + chunk_seconds）向前在 CUT_LOOKBACK
        窗口内找最靠后且时长 >= MIN_SILENCE 的静音段，取其中点为切点；
        找不到（连续长配乐/对白）时退化为硬切并告警。

        返回切点列表 [0, c1, ..., 时长]；silero-vad 不可用或检测失败时
        返回 None，调用方回退固定步进切块。
        """
        try:
            from silero_vad import get_speech_timestamps, load_silero_vad
        except ImportError:
            logger.warning("silero-vad 未安装，回退固定步进切块（pip install silero-vad）")
            return None

        try:
            wav, sr = sf.read(wav16k_path, dtype="float32")
            if wav.ndim > 1:
                wav = wav[:, 0]
            duration = len(wav) / sr
            speech = get_speech_timestamps(
                torch.from_numpy(wav), load_silero_vad(), sampling_rate=sr
            )
        except Exception:
            logger.exception("VAD 静音检测失败，回退固定步进切块")
            return None

        # 语音区间的补集即静音段（时长 >= MIN_SILENCE 才可作为切点候选）
        silences: list[tuple[float, float]] = []
        prev_end = 0.0
        for seg in speech:
            s, e = seg["start"] / sr, seg["end"] / sr
            if s - prev_end >= MIN_SILENCE:
                silences.append((prev_end, s))
            prev_end = e
        if duration - prev_end >= MIN_SILENCE:
            silences.append((prev_end, duration))

        cut_points = [0.0]
        cur = 0.0
        while duration - cur > chunk_seconds + CHUNK_OVERLAP:
            target = cur + chunk_seconds
            # 取静音段与回看窗口的交集（长静音可能横跨目标点），
            # 静音段有序，最后一个满足的即最靠近目标点；切点取交集中点
            best: Optional[tuple[float, float]] = None
            for s, e in silences:
                lo = max(s, target - CUT_LOOKBACK)
                hi = min(e, target)
                if hi - lo >= MIN_SILENCE:
                    best = (lo, hi)
            if best is not None:
                cut_points.append((best[0] + best[1]) / 2)
            else:
                logger.warning(
                    "目标切点 %.0fs 前 %.0fs 内未找到 >=%.1fs 的静音段，硬切（跨句风险）",
                    target, CUT_LOOKBACK, MIN_SILENCE,
                )
                cut_points.append(target)
            cur = cut_points[-1]
        cut_points.append(duration)
        return cut_points
