"""POC: MOSS-Transcribe-Diarize 本地 CPU 推理验证.

环境: Windows + Python 3.13.5 + torch 2.14.0+cpu + transformers 5.17.0
模型: models/asr/MOSS-Transcribe-Diarize (trust_remote_code=True)
音频: poc/test_audio.wav (System.Speech TTS 生成, 约 9 秒英文)

输出: 原始转录文本 + 解析后的 [start][Sxx]text[end] 分段,
      以及加载/预处理/生成耗时与峰值内存。
"""

from __future__ import annotations

import re
import sys
import threading
import time
from pathlib import Path

import psutil
import torch
from transformers import AutoModelForCausalLM, AutoProcessor
from transformers.audio_utils import load_audio

ROOT = Path(__file__).resolve().parent.parent
MODEL_DIR = ROOT / "models" / "asr" / "MOSS-Transcribe-Diarize"
AUDIO_PATH = Path(__file__).resolve().parent / "test_audio.wav"

# 模型卡默认提示词(优化用于时间戳+说话人分离转写)
DEFAULT_PROMPT = (
    "请将音频转写为文本，每一段需以起始时间戳和说话人编号"
    "（[S01]、[S02]、[S03]…）开头，正文为对应的语音内容，"
    "并在段末标注结束时间戳，以清晰标明该段语音范围。"
)

SEGMENT_RE = re.compile(r"\[(\d+(?:\.\d+)?)\]\[(S\d+)\](.*?)\[(\d+(?:\.\d+)?)\]", re.S)


def parse_transcript(text: str) -> list[dict]:
    return [
        {"start": float(a), "speaker": sp, "text": body.strip(), "end": float(b)}
        for a, sp, body, b in SEGMENT_RE.findall(text)
    ]


def build_messages(audio_path: str | Path, prompt: str = DEFAULT_PROMPT) -> list[dict]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "audio", "audio": str(audio_path)},
                {"type": "text", "text": prompt},
            ],
        }
    ]


class PeakMemoryMonitor(threading.Thread):
    """采样本进程 RSS 峰值."""

    def __init__(self, interval: float = 0.2):
        super().__init__(daemon=True)
        self.proc = psutil.Process()
        self.interval = interval
        self.peak = 0
        self._stop = threading.Event()

    def run(self):
        while not self._stop.is_set():
            rss = self.proc.memory_info().rss
            if rss > self.peak:
                self.peak = rss
            time.sleep(self.interval)

    def stop(self):
        self._stop.set()
        self.join(timeout=2)


def main() -> None:
    dtype_name = sys.argv[1] if len(sys.argv) > 1 else "fp32"
    dtype = torch.bfloat16 if dtype_name == "bf16" else torch.float32
    print(f"[env] python torch={torch.__version__} device=cpu dtype={dtype_name}")
    print(f"[model] {MODEL_DIR}")
    mon = PeakMemoryMonitor()
    mon.start()

    # ---- 1. 加载模型与处理器 (remote code) ----
    t0 = time.perf_counter()
    model = AutoModelForCausalLM.from_pretrained(
        MODEL_DIR,
        trust_remote_code=True,
        dtype=dtype,
        attn_implementation="sdpa",
    ).eval()
    processor = AutoProcessor.from_pretrained(MODEL_DIR, trust_remote_code=True)
    t_load = time.perf_counter() - t0
    print(f"[load] {t_load:.1f}s  rss_after_load={mon.peak / 2**30:.2f} GiB")

    # ---- 2. 构建 messages -> chat template -> 特征 ----
    messages = build_messages(AUDIO_PATH)
    t1 = time.perf_counter()
    text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    audio = load_audio(str(AUDIO_PATH), sampling_rate=processor.feature_extractor.sampling_rate)
    inputs = processor(text=text, audio=[audio], max_length=131072, return_tensors="pt")
    t_prep = time.perf_counter() - t1
    prompt_len = int(inputs["attention_mask"][0].sum())
    print(f"[prep] {t_prep:.1f}s  prompt_tokens={prompt_len}  audio_samples={audio.shape[0]}")

    # ---- 3. 生成 ----
    t2 = time.perf_counter()
    with torch.inference_mode():
        outputs = model.generate(
            input_ids=inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            input_features=inputs["input_features"],
            audio_feature_lengths=inputs["audio_feature_lengths"],
            audio_chunk_mapping=inputs["audio_chunk_mapping"],
            max_new_tokens=2048,
            do_sample=False,
        )
    t_gen = time.perf_counter() - t2

    gen_ids = outputs[0][prompt_len:]
    result = processor.tokenizer.decode(gen_ids, skip_special_tokens=True).strip()
    mon.stop()

    print(f"[gen] {t_gen:.1f}s  new_tokens={gen_ids.numel()}  speed={gen_ids.numel() / t_gen:.2f} tok/s")
    print(f"[mem] peak_rss={mon.peak / 2**30:.2f} GiB")
    print("[raw text]")
    print(result)
    print("[segments]")
    for seg in parse_transcript(result):
        print(f"  {seg['start']:>7.2f} - {seg['end']:<7.2f}  {seg['speaker']}: {seg['text']}")


if __name__ == "__main__":
    main()
