# -*- coding: utf-8 -*-
"""流水线前半段端到端验证脚本。

覆盖模块：segment / audio_extract / separate / asr / llm_engine /
proofread / translate / voice_pick。

步骤：
1. extract_audio(poc/test_mixed.wav) -> full_audio.wav
2. separate_vocals -> vocals.wav / no_vocals.wav
3. ASRModule 加载 + transcribe(poc/test_audio.wav) -> Segment 列表
4. LlamaServerManager 启动 + LLMClient 翻译 3 条测试句
5. proofread_segments + translate_segments 端到端（断言时间戳不变）
6. voice_pick 生成 ref_S01.wav（无 3s+ 片段则放宽 min_dur=1.0）
7. 停止 llama-server、卸载 ASR 模型
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import soundfile as sf

from app.core.hardware import detect_device
from app.core.segment import Segment, segments_from_json, segments_to_json
from app.pipeline.asr import ASRModule
from app.pipeline.audio_extract import extract_audio
from app.pipeline.llm_engine import LLMClient, LlamaServerManager
from app.pipeline.proofread import proofread_segments
from app.pipeline.separate import separate_vocals
from app.pipeline.translate import translate_segments
from app.pipeline.voice_pick import pick_reference_segments

WORK = ROOT / "poc" / "test_front_workdir"
MODEL_ASR = ROOT / "models" / "asr" / "MOSS-Transcribe-Diarize"
MODEL_LLM = ROOT / "models" / "llm" / "Qwen3-4B-GGUF" / "Qwen3-4B-Q4_K_M.gguf"
DEVICE = detect_device().device


class DemoProgress:
    """演示进度回调：同一条消息只打一次，分步打印避免刷屏。"""

    def __init__(self) -> None:
        self._last_frac = -1.0

    def __call__(self, frac: float, msg: str) -> None:
        # 分数前进 >= 0.25 或消息变化时打印
        if frac - self._last_frac >= 0.25 or msg != getattr(self, "_last_msg", None):
            print(f"    [progress] {frac:.2f} {msg}", flush=True)
        self._last_frac = frac
        self._last_msg = msg


def step(title: str) -> None:
    print("\n" + "=" * 64, flush=True)
    print(f"[STEP] {title}", flush=True)
    print("=" * 64, flush=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="  %(levelname)s %(name)s: %(message)s")
    WORK.mkdir(parents=True, exist_ok=True)
    t_start = time.perf_counter()

    # ---- 1. extract_audio ----
    step("1. extract_audio: poc/test_mixed.wav -> full_audio.wav")
    full_audio = extract_audio(str(ROOT / "poc" / "test_mixed.wav"), WORK, progress=DemoProgress())
    info = sf.info(full_audio)
    print(f"  -> {full_audio}  sr={info.samplerate} ch={info.channels} dur={info.duration:.2f}s")
    assert Path(full_audio).is_file() and info.duration > 8

    # ---- 2. separate_vocals ----
    step(f"2. separate_vocals: 分离人声/伴奏（device={DEVICE}）")
    t0 = time.perf_counter()
    vocals, no_vocals = separate_vocals(full_audio, WORK, device=DEVICE, progress=DemoProgress())
    print(f"  -> 耗时 {time.perf_counter() - t0:.1f}s")
    for p in (vocals, no_vocals):
        i = sf.info(p)
        print(f"  -> {p}  sr={i.samplerate} ch={i.channels} dur={i.duration:.2f}s")
        assert Path(p).is_file() and i.duration > 8

    # ---- 3. ASR ----
    step(f"3. ASRModule: 加载 + transcribe(poc/test_audio.wav)（device={DEVICE}）")
    asr = ASRModule()
    t0 = time.perf_counter()
    asr.load(str(MODEL_ASR), device=DEVICE)
    print(f"  -> 模型加载 {time.perf_counter() - t0:.1f}s")
    t0 = time.perf_counter()
    segments = asr.transcribe(str(ROOT / "poc" / "test_audio.wav"), progress=DemoProgress())
    print(f"  -> 推理耗时 {time.perf_counter() - t0:.1f}s，共 {len(segments)} 段：")
    for s in segments:
        print(f"     {s.spk} [{s.start:6.2f} ~ {s.end:6.2f}] {s.text}")
    assert len(segments) > 0, "ASR 未产出分段"
    assert all(s.text.strip() for s in segments), "存在空文本分段"
    assert len({s.spk for s in segments}) == 1, "测试音频应为单说话人"
    assert all(s.end > s.start for s in segments), "时间戳异常"

    # segment JSON 序列化往返
    roundtrip = segments_from_json(segments_to_json(segments))
    assert [(s.spk, s.start, s.end, s.text) for s in roundtrip] == [
        (s.spk, s.start, s.end, s.text) for s in segments
    ], "Segment JSON 往返不一致"
    print("  -> Segment JSON 序列化往返一致")

    # ---- 4. LLM server + 翻译测试 ----
    step("4. LlamaServerManager + LLMClient: 3 条测试句翻译")
    server = LlamaServerManager()
    asr.unload()  # 提前释放 ASR 内存，给 LLM 让路
    print("  -> ASR 模型已卸载")
    try:
        t0 = time.perf_counter()
        server.start(str(MODEL_LLM), port=8817, threads=8, progress=DemoProgress())
        print(f"  -> llama-server 就绪，耗时 {time.perf_counter() - t0:.1f}s")
        client = LLMClient("http://127.0.0.1:8817")

        sys_prompt = (
            "你是字幕翻译。把用户给出的英文字幕逐句翻译成简洁自然的简体中文，"
            "只输出译文，每句一行，不要编号，不要解释。"
        )
        t0 = time.perf_counter()
        reply = client.chat(
            sys_prompt,
            "请翻译以下三句英文字幕：\n"
            "1. I don't think we have much of a choice here.\n"
            "2. You gotta be kidding me.\n"
            "3. Let's just call it a day and grab some dinner.",
            temperature=0.2,
            max_tokens=512,
        )
        print(f"  -> 翻译耗时 {time.perf_counter() - t0:.1f}s，译文：\n{reply}")
        assert any("\u4e00" <= ch <= "\u9fff" for ch in reply), "译文不含中文"

        # ---- 5. proofread + translate 端到端 ----
        step("5. proofread_segments + translate_segments（ASR 真实输出）")
        key_of = lambda ss: [(x.spk, round(x.start, 3), round(x.end, 3)) for x in ss]

        t0 = time.perf_counter()
        proofread = proofread_segments(client, segments, progress=DemoProgress())
        print(f"  -> 校对耗时 {time.perf_counter() - t0:.1f}s")
        assert key_of(proofread) == key_of(segments), "校对后时间戳/spk 发生变化"
        for a, b in zip(segments, proofread):
            mark = "(deleted)" if b.deleted else ""
            print(f"     原文: {a.text}\n     校对: {b.text} {mark}")

        t0 = time.perf_counter()
        translated = translate_segments(
            client, proofread, source_lang="英语", target_lang="简体中文",
            progress=DemoProgress(),
        )
        print(f"  -> 翻译耗时 {time.perf_counter() - t0:.1f}s")
        assert key_of(translated) == key_of(proofread), "翻译后时间戳/spk 发生变化"
        for s in translated:
            if not s.deleted:
                assert s.target_text.strip(), f"存在未翻译段: {s.text}"
            print(f"     {s.spk} [{s.start:.2f}~{s.end:.2f}] {s.text} => {s.target_text}")

        # 原列表不变
        assert all(not s.target_text for s in segments), "translate/proofread 修改了原列表"

        # ---- 6. voice_pick ----
        step("6. pick_reference_segments: 挑参考音色片段")
        refs = pick_reference_segments(
            translated, vocals, WORK, min_dur=3.0, max_dur=12.0, progress=DemoProgress()
        )
        if "S01" not in refs:
            print("  -> 无 3s+ 片段，放宽 min_dur=1.0 重试")
            refs = pick_reference_segments(
                translated, vocals, WORK, min_dur=1.0, max_dur=12.0, progress=DemoProgress()
            )
        print(f"  -> refs = {refs}")
        assert "S01" in refs, "未生成 S01 参考音频"
        ri = sf.info(refs["S01"])
        print(f"  -> {refs['S01']}  sr={ri.samplerate} ch={ri.channels} dur={ri.duration:.2f}s")
        assert ri.samplerate == 16000 and ri.channels == 1

    finally:
        # ---- 7. 停止 llama-server ----
        step("7. 停止 llama-server")
        server.stop()
        assert not server.is_running, "llama-server 未停止"
        print("  -> llama-server 已停止")

    print("\n" + "=" * 64)
    print(f"[OK] 全部验证通过，总耗时 {time.perf_counter() - t_start:.1f}s")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
