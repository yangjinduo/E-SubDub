# -*- coding: utf-8 -*-
"""ML 推理子进程 worker：demucs 人声分离 + MOSS ASR 转写（运行于 .venv-ml）。

为什么独立子进程：IndexTTS 需要 transformers 4.x，MOSS ASR 的 remote code
按 transformers 5.x 编写，两者无法共存于同一环境；打包发布后主程序为
Nuitka 冻结可执行（无运行时 pip 能力），故 ASR/demucs 所需的
torch+transformers 5.x 独立安装在 .venv-ml，本 worker 由
app/pipeline/ml_client.py 用 .venv-ml 的 Python 启动（开发态直接用主
环境解释器运行，行为等价）。本文件必须自包含：不得 import app 包
（冻结态 app 包被编译进可执行文件，磁盘上没有对应源码）。

协议（JSON Lines，一行一条；与 tts_worker.py 同构）：
- 通道：stdin 收请求，原 stdout 回响应；第三方库的全部 stdout 输出在
  启动时重定向到 stderr（父进程泵出后转发界面日志），协议通道保持纯净；
- 启动参数：--ffmpeg <内置 ffmpeg 路径>（ASR 长音频切块用）；
- 启动完成：{"event": "ready"}；
- 请求 {"cmd": "separate", "id": int, "audio": str, "work_dir": str,
         "device": str}
  进度 {"id": int, "event": "progress", "frac": 0~1, "desc": str}
  完成 {"id": int, "ok": true, "vocals": str, "no_vocals": str}
  失败 {"id": int, "ok": false, "error": str, "kind": "memory"|"error"}；
- 请求 {"cmd": "asr_load", "id": int, "model_dir": str, "device": str}
  → {"id": int, "ok": true} / {"id": int, "ok": false, "error", "kind"}；
- 请求 {"cmd": "asr_transcribe", "id": int, "wav": str,
         "chunk_minutes": int}
  → 进度事件 → {"id": int, "ok": true,
                 "segments": [[spk, start, end, text], …]} / 失败同上；
- 请求 {"cmd": "asr_unload", "id": int} → {"id": int, "ok": true}；
- {"cmd": "shutdown"} 或 stdin 关闭即退出（显存随进程退出释放）。
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import re
import subprocess
import sys
import tempfile
import types
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import transformers
from transformers import AutoModelForCausalLM, AutoProcessor
from transformers.audio_utils import load_audio
from transformers.generation.streamers import BaseStreamer


# ---------------------------------------------------------------- 兼容与工具

_TF_MAJOR = int(transformers.__version__.split(".")[0])

_COMPAT_DONE = False


def _ensure_transformers_compat() -> None:
    """transformers 4.52/5.x 兼容垫片（幂等），照 app/pipeline/asr.py。"""
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


def _redirect_stdout_to_stderr() -> int:
    """fd 1 → fd 2，返回原 stdout 副本 fd 作为协议通道（照 tts_worker.py）。

    transformers / demucs 的加载与推理会向 stdout 打印进度信息，
    不重定向会混入 JSON Lines 协议导致父进程解析失败。
    """
    saved = os.dup(1)
    os.dup2(2, 1)
    sys.stdout = os.fdopen(2, "w", encoding="utf-8", errors="replace", buffering=1)
    return saved


def _classify(exc: BaseException) -> str:
    """把异常归类为 memory（内存/显存不足）或 error（其余）。"""
    text = f"{exc}"
    if isinstance(exc, MemoryError):
        return "memory"
    if getattr(exc, "winerror", None) == 1455 or "1455" in text:
        return "memory"  # Windows 页面文件不足
    lowered = text.lower()
    if type(exc).__module__.startswith("torch") and "out of memory" in lowered:
        return "memory"  # torch.cuda.OutOfMemoryError
    if "out of memory" in lowered or "page file" in lowered:
        return "memory"
    return "error"


# ---------------------------------------------------------------- demucs 分离

MODEL_NAME = "htdemucs"


def _load_demucs_model(name: str = MODEL_NAME):
    """加载 demucs 预训练模型（legacy 仓库优先，直连 AWS 源下载）。

    优先走 demucs 官方 legacy 仓库路径（直接命中 torch hub 缓存，无网络
    请求；未缓存时从官方 AWS 源下载）；失败再退回标准 get_model()。
    这样在无法直连 huggingface.co 的环境下不会浪费约 25 秒重试。
    """
    from demucs.pretrained import REMOTE_ROOT, _parse_remote_files, get_model
    from demucs.repo import AnyModelRepo, BagOnlyRepo, RemoteRepo

    try:
        models = _parse_remote_files(REMOTE_ROOT / "files.txt")
        model_repo = RemoteRepo(models)
        bag_repo = BagOnlyRepo(REMOTE_ROOT, model_repo)
        repo = AnyModelRepo(model_repo, bag_repo)
        model = repo.get_model(name)
        model.eval()
        return model
    except Exception as exc:
        print(f"[ml-worker] legacy 仓库加载 {name} 失败（{exc!r}），改用标准 get_model",
              file=sys.stderr)
        return get_model(name)


def _run_separate(req: dict, send) -> None:
    """人声分离请求处理：two-stems vocals，输出 vocals/no_vocals 两个 wav。

    逻辑与原 app/pipeline/separate.py 一致：htdemucs 模型、自动分块
    （split=True + 块间 0.25 重叠）、整体幅值归一化、44.1kHz 立体声 PCM16。
    """
    from demucs.apply import apply_model
    from demucs.audio import convert_audio

    def _progress(frac: float, desc: str) -> None:
        send({"id": req["id"], "event": "progress",
              "frac": float(frac), "desc": str(desc)})

    audio_path = str(req["audio"])
    work_dir = Path(req["work_dir"])
    work_dir.mkdir(parents=True, exist_ok=True)
    vocals_path = work_dir / "vocals.wav"
    no_vocals_path = work_dir / "no_vocals.wav"

    _progress(0.02, f"加载人声分离模型 {MODEL_NAME}")
    model = _load_demucs_model(MODEL_NAME)
    target_device = torch.device(str(req.get("device") or "cpu"))

    # 读取音频并适配模型采样率/声道（参考 demucs.api.Separator 写法）
    data, sr = sf.read(audio_path, dtype="float32", always_2d=True)
    wav = torch.from_numpy(np.ascontiguousarray(data.T))  # (C, T)
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
            _progress(
                0.1 + 0.85 * min(offset / length, 0.99),
                f"人声分离中（已处理 {offset / model.samplerate:.0f}/"
                f"{length / model.samplerate:.0f} 秒）",
            )

    _progress(0.08, "人声分离推理中")
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

    # two-stems：vocals 单独输出，其余 stem 求和为 no_vocals（CLI 默认）
    sources = dict(zip(model.sources, out[0]))
    if "vocals" not in sources:
        raise RuntimeError(f"模型 {MODEL_NAME} 不含 vocals 音源: {model.sources}")
    vocals = sources["vocals"]
    others = [sources[name] for name in model.sources if name != "vocals"]
    no_vocals = torch.stack(others, dim=0).sum(dim=0)

    _progress(0.95, "保存人声/伴奏音频")
    sf.write(str(vocals_path), np.ascontiguousarray(vocals.T.numpy()),
             model.samplerate, subtype="PCM_16")
    sf.write(str(no_vocals_path), np.ascontiguousarray(no_vocals.T.numpy()),
             model.samplerate, subtype="PCM_16")
    send({"id": req["id"], "ok": True,
          "vocals": str(vocals_path), "no_vocals": str(no_vocals_path)})


# ---------------------------------------------------------------- MOSS ASR

# 模型卡默认提示词（优化用于时间戳 + 说话人分离转写）
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)

# 解析模型输出行：[0.00][S01] hello world[2.30]
SEGMENT_RE = re.compile(r"\[(\d+(?:\.\d+)?)\]\[(S\d+)\](.*?)\[(\d+(?:\.\d+)?)\]", re.S)

# 块间重叠（秒）：回退固定步进切块时相邻块重叠区用于去重拼接
CHUNK_OVERLAP = 10.0

# 转写输出 token 密度（token/秒音频）：中文多说话人实测约 8.9，留余量取 12
TOKENS_PER_SECOND = 12
TOKENS_BASE = 256
MAX_NEW_TOKENS_CAP = 16384

# C 点切块：候选静音段最短时长（秒）与从目标切点向前找静音的窗口（秒）
MIN_SILENCE = 0.3
CUT_LOOKBACK = 90.0


class _CountingStreamer(BaseStreamer):
    """token 计数流式器：按已生成的新 token 数触发回调（转写进度用）。"""

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
    import difflib

    na, nb = _normalize_text(a), _normalize_text(b)
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    return difflib.SequenceMatcher(None, na, nb).ratio() >= 0.5


def _time_overlaps(a: dict, b: dict, eps: float = 0.2) -> bool:
    """两段时间戳是否重叠（a/b 为 {"start","end"} dict）。"""
    return a["start"] < b["end"] - eps and b["start"] < a["end"] - eps


class _ASRState:
    """已加载的 MOSS 模型（worker 进程内单实例）。"""

    def __init__(self) -> None:
        self.model = None
        self.processor = None
        self.device = "cpu"

    @property
    def loaded(self) -> bool:
        return self.model is not None


_asr = _ASRState()


def _asr_load(req: dict, send) -> None:
    """加载 MOSS-Transcribe-Diarize（bf16 + sdpa + trust_remote_code）。"""
    if _asr.loaded:
        send({"id": req["id"], "ok": True})
        return
    device = str(req.get("device") or "cpu")
    model_dir = str(req["model_dir"])

    send({"id": req["id"], "event": "progress",
          "frac": 0.02, "desc": "加载转写模型权重"})
    # 4.x 用 torch_dtype；5.x 用 dtype（字符串规避 config repr 的序列化 bug）
    dtype_kwargs = (
        {"dtype": "bfloat16"} if _TF_MAJOR >= 5 else {"torch_dtype": torch.bfloat16}
    )
    _asr.model = AutoModelForCausalLM.from_pretrained(
        model_dir,
        trust_remote_code=True,
        attn_implementation="sdpa",
        **dtype_kwargs,
    ).eval()
    if device != "cpu":
        _asr.model.to(device)
    _asr.processor = AutoProcessor.from_pretrained(model_dir, trust_remote_code=True)
    _asr.device = device

    # 4.52 不会读取独立的 chat_template.jinja（5.x 约定），手动补挂
    tokenizer = getattr(_asr.processor, "tokenizer", None)
    if tokenizer is not None and not getattr(tokenizer, "chat_template", None):
        tmpl = Path(model_dir) / "chat_template.jinja"
        if tmpl.is_file():
            tokenizer.chat_template = tmpl.read_text(encoding="utf-8")

    # transformers 4.52 生成循环不传 is_first_iteration（remote code 按 5.x
    # 编写），导致首步注入的音频特征进不了模型、输出乱码；打运行时补丁：
    # past_key_values 为空（生成首步）时强制 is_first_iteration=True
    if _TF_MAJOR < 5:
        orig_pifg = type(_asr.model).prepare_inputs_for_generation

        def _pifg_compat(model_self, input_ids, *args, **kwargs):
            past = kwargs.get("past_key_values")
            if past is None or (
                hasattr(past, "get_seq_length") and past.get_seq_length() == 0
            ):
                kwargs["is_first_iteration"] = True
            return orig_pifg(model_self, input_ids, *args, **kwargs)

        _asr.model.prepare_inputs_for_generation = types.MethodType(
            _pifg_compat, _asr.model
        )
    send({"id": req["id"], "ok": True})


def _asr_unload(req: dict, send) -> None:
    """释放 MOSS 模型与显存。"""
    _asr.model = None
    _asr.processor = None
    gc.collect()
    if _asr.device != "cpu" and torch.cuda.is_available():
        torch.cuda.empty_cache()
    send({"id": req["id"], "ok": True})


def _transcribe_chunk(wav_path: str, audio_seconds: float, on_tokens=None) -> list[dict]:
    """对单个音频文件推理，返回解析后的分段（时间相对该文件起点）。"""
    processor = _asr.processor
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
    if _asr.device != "cpu":
        inputs = {k: (v.to(_asr.device) if hasattr(v, "to") else v) for k, v in inputs.items()}
    prompt_len = int(inputs["attention_mask"][0].sum())

    streamer = _CountingStreamer(on_tokens) if on_tokens is not None else None
    with torch.inference_mode():
        outputs = _asr.model.generate(
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
    # 截断检测：生成数达到预算且末 token 不是 EOS，块尾内容可能被截断
    if gen_ids.numel() >= budget:
        eos_id = processor.tokenizer.eos_token_id
        eos_ids = eos_id if isinstance(eos_id, (list, tuple)) else [eos_id]
        if int(gen_ids[-1]) not in eos_ids:
            print(
                f"[ml-worker] 转写输出达到 max_new_tokens={budget} 上限"
                f"（{audio_seconds:.1f} 秒音频），该块末尾内容可能被截断: {wav_path}",
                file=sys.stderr,
            )
    result = processor.tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
    return [
        {"start": float(a), "spk": sp, "text": body.strip(), "end": float(b)}
        for a, sp, body, b in SEGMENT_RE.findall(result)
    ]


def _cut_chunk(wav_path: str, start: float, length: float, out_path: Path,
               ffmpeg: str) -> None:
    """用 ffmpeg 从 wav 中切块（16kHz 单声道，降低临时文件体积）。"""
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


def _merge_chunk(merged: list[dict], chunk_segs: list[dict]) -> None:
    """把一块的分段合并进全局列表（重叠区去重，保留后块版本）。"""
    for seg in chunk_segs:
        replaced = False
        for j, existing in enumerate(merged):
            if _time_overlaps(existing, seg) and _texts_similar(existing["text"], seg["text"]):
                merged[j] = seg
                replaced = True
                break
        if not replaced:
            merged.append(seg)


def _find_cut_points(wav16k_path: str, chunk_seconds: float):
    """C 点切块：用 silero-vad 找静音间隙作为切点，句子不跨块。

    返回切点列表 [0, c1, …, 时长]；silero-vad 不可用或检测失败时
    返回 None，调用方回退固定步进切块。
    """
    try:
        from silero_vad import get_speech_timestamps, load_silero_vad
    except ImportError:
        print("[ml-worker] silero-vad 未安装，回退固定步进切块", file=sys.stderr)
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
        print("[ml-worker] VAD 静音检测失败，回退固定步进切块", file=sys.stderr)
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
        # 取静音段与回看窗口的交集，最后一个满足的即最靠近目标点
        best = None
        for s, e in silences:
            lo = max(s, target - CUT_LOOKBACK)
            hi = min(e, target)
            if hi - lo >= MIN_SILENCE:
                best = (lo, hi)
        if best is not None:
            cut_points.append((best[0] + best[1]) / 2)
        else:
            print(
                f"[ml-worker] 目标切点 {target:.0f}s 前 {CUT_LOOKBACK:.0f}s 内未找到 "
                f">={MIN_SILENCE:.1f}s 的静音段，硬切（跨句风险）",
                file=sys.stderr,
            )
            cut_points.append(target)
        cur = cut_points[-1]
    cut_points.append(duration)
    return cut_points


def _run_asr_transcribe(req: dict, send, ffmpeg: str) -> None:
    """转写 wav 文件：短音频单块直推；长音频切块后逐块推理再合并。

    说话人标签按块顺序全局偏移重编号；生成 token 预算按块时长动态估算
    （逻辑与原 app/pipeline/asr.py 一致）。
    """
    wav_path = str(req["wav"])
    chunk_minutes = int(req.get("chunk_minutes") or 8)
    rid = req["id"]

    def _progress(frac: float, desc: str) -> None:
        send({"id": rid, "event": "progress",
              "frac": float(frac), "desc": str(desc)})

    def _token_progress(frac_from: float, frac_to: float, base_msg: str, budget: int):
        """token 计数 → 进度：映射到 [frac_from, frac_to)，10% 步进附百分比。"""
        def on_tokens(count: int) -> None:
            frac = min(count / budget, 1.0)
            pct = min(int(frac * 10), 9) * 10
            _progress(frac_from + (frac_to - frac_from) * frac,
                      f"{base_msg}{pct}%" if pct else base_msg)
        return on_tokens

    info = sf.info(wav_path)
    duration = info.frames / info.samplerate
    chunk_seconds = chunk_minutes * 60.0

    # 单块即可容纳：直接推理
    if duration <= chunk_seconds + CHUNK_OVERLAP:
        base_msg = f"转写中（{duration:.1f} 秒音频）"
        _progress(0.1, base_msg)
        budget = min(int(duration * TOKENS_PER_SECOND) + TOKENS_BASE, MAX_NEW_TOKENS_CAP)
        raw = _transcribe_chunk(
            wav_path, duration, _token_progress(0.1, 0.9, base_msg, budget)
        )
        _progress(0.95, "转写完成，解析分段")
        segments = sorted(raw, key=lambda s: s["start"])
        _progress(1.0, f"转写完成，共 {len(segments)} 段")
        send({"id": rid, "ok": True, "segments": [
            [s["spk"], s["start"], s["end"], s["text"]] for s in segments
        ]})
        return

    # 长音频：切块处理。VAD 检测阶段占 [0, VAD_FRAC)，各块占余下区间
    VAD_FRAC = 0.05
    merged: list[dict] = []
    spk_offset = 0  # 已使用的全局说话人编号数
    with tempfile.TemporaryDirectory(prefix="asr_chunks_") as tmp:
        tmp_dir = Path(tmp)
        # 统一转 16k 单声道中间文件：VAD 与切块共用，避免重复转码
        full16k = tmp_dir / "full_16k.wav"
        _cut_chunk(wav_path, 0.0, duration, full16k, ffmpeg)
        _progress(VAD_FRAC * 0.5, "检测静音切点（VAD）")
        cut_points = _find_cut_points(str(full16k), chunk_seconds)
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
            blocks = [(s, min(chunk_seconds, duration - s)) for s in starts]
            vad_frac = 0.0
        _progress(vad_frac, "切块完成，开始逐块转写")
        total = len(blocks)

        for i, (chunk_start, chunk_len) in enumerate(blocks):
            chunk_file = tmp_dir / f"chunk_{i:04d}.wav"
            _cut_chunk(str(full16k), chunk_start, chunk_len, chunk_file, ffmpeg)
            chunk_msg = (
                f"转写块 {i + 1}/{total}"
                f"（{chunk_start:.0f}~{chunk_start + chunk_len:.0f} 秒）"
            )
            # 该块的切块/生成/合并各占块内进度的 0~0.1 / 0.1~0.9 / 0.9~1
            frac_a = vad_frac + (1.0 - vad_frac) * (i + 0.1) / total
            frac_b = vad_frac + (1.0 - vad_frac) * (i + 0.9) / total
            budget = min(int(chunk_len * TOKENS_PER_SECOND) + TOKENS_BASE, MAX_NEW_TOKENS_CAP)
            raw = _transcribe_chunk(
                str(chunk_file), chunk_len,
                _token_progress(frac_a, frac_b, chunk_msg, budget),
            )
            chunk_file.unlink(missing_ok=True)

            # 块内局部标签 -> 全局偏移标签
            local_max = 0
            chunk_segs: list[dict] = []
            for item in raw:
                m = re.fullmatch(r"S(\d+)", item["spk"])
                local_idx = int(m.group(1)) if m else 1
                local_max = max(local_max, local_idx)
                chunk_segs.append({
                    "spk": f"S{spk_offset + local_idx:02d}",
                    "start": chunk_start + item["start"],
                    "end": chunk_start + item["end"],
                    "text": item["text"],
                })
            spk_offset += local_max

            _merge_chunk(merged, chunk_segs)
            _progress(vad_frac + (1.0 - vad_frac) * (i + 1) / total, chunk_msg)

    merged.sort(key=lambda s: s["start"])
    _progress(1.0, f"转写完成，共 {len(merged)} 段")
    send({"id": rid, "ok": True, "segments": [
        [s["spk"], s["start"], s["end"], s["text"]] for s in merged
    ]})


# ---------------------------------------------------------------- 主循环

def main() -> int:
    parser = argparse.ArgumentParser(description="demucs + MOSS ASR worker")
    parser.add_argument("--ffmpeg", required=True, help="内置 ffmpeg 可执行路径")
    args = parser.parse_args()

    proto_fd = _redirect_stdout_to_stderr()
    proto = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
    stdin = sys.stdin

    def send(obj: dict) -> None:
        try:
            proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except (BrokenPipeError, OSError):
            raise SystemExit(2)  # 父进程已退出

    send({"event": "ready"})

    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        cmd = req.get("cmd")
        if cmd == "shutdown":
            break
        try:
            if cmd == "separate":
                _run_separate(req, send)
            elif cmd == "asr_load":
                _asr_load(req, send)
            elif cmd == "asr_transcribe":
                if not _asr.loaded:
                    raise RuntimeError("ASR 模型未加载（需先 asr_load）")
                _run_asr_transcribe(req, send, args.ffmpeg)
            elif cmd == "asr_unload":
                _asr_unload(req, send)
            else:
                send({"id": req.get("id"), "ok": False,
                      "error": f"未知命令: {cmd}", "kind": "error"})
        except SystemExit:
            raise
        except BaseException as exc:  # noqa: BLE001 - 原样上报父进程
            send({
                "id": req.get("id"), "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "kind": _classify(exc),
            })
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
