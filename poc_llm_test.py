# -*- coding: utf-8 -*-
"""POC: Qwen3-4B-GGUF + llama.cpp llama-server CPU 推理验证脚本"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8817"
URL = BASE + "/v1/chat/completions"
HEADERS = {"Content-Type": "application/json"}


def wait_health(timeout=300):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            r = requests.get(BASE + "/health", timeout=5)
            if r.status_code == 200 and r.json().get("status") == "ok":
                return True
        except Exception:
            pass
        time.sleep(2)
    return False


def run_chat(name, messages, max_tokens=512):
    payload = {
        "model": "qwen3-4b",
        "messages": messages,
        "temperature": 0.3,
        "max_tokens": max_tokens,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    t0 = time.time()
    ttft = None
    parts = []
    usage = None
    with requests.post(URL, headers=HEADERS, json=payload, stream=True, timeout=600) as r:
        r.raise_for_status()
        for line in r.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                break
            try:
                obj = json.loads(data)
            except json.JSONDecodeError:
                continue
            if obj.get("usage"):
                usage = obj["usage"]
            choices = obj.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                if content:
                    if ttft is None:
                        ttft = time.time() - t0
                    parts.append(content)
    total = time.time() - t0
    text = "".join(parts)
    comp_tokens = (usage or {}).get("completion_tokens", len(parts))
    prompt_tokens = (usage or {}).get("prompt_tokens")
    tok_s = comp_tokens / (total - ttft) if ttft and total > ttft else 0
    print("=" * 60)
    print(f"[{name}] TTFT={ttft:.2f}s  total={total:.2f}s  completion_tokens={comp_tokens}  prompt_tokens={prompt_tokens}")
    print(f"[{name}] gen speed={tok_s:.2f} tok/s (excl. TTFT)  end-to-end={comp_tokens/total:.2f} tok/s")
    print(f"[{name}] RESPONSE:\n{text}\n")
    return text, ttft, total, comp_tokens, tok_s


def main():
    if not wait_health():
        print("FATAL: server /health not ready in 300s")
        sys.exit(1)
    print("server ready")

    # 测试 a: 字幕英->中翻译 (含口语俚语)
    trans_msgs = [
        {
            "role": "system",
            "content": "你是字幕翻译。把用户给出的英文字幕逐句翻译成简洁自然的简体中文，只输出译文，每句一行，不要编号，不要解释。",
        },
        {
            "role": "user",
            "content": (
                "/no_think\n请翻译以下三句英文字幕：\n"
                "1. I don't think we have much of a choice here.\n"
                "2. You gotta be kidding me.\n"
                "3. Let's just call it a day and grab some dinner."
            ),
        },
    ]
    run_chat("A-translation", trans_msgs, max_tokens=256)

    # 测试 b: ASR 错误文本 JSON 修复
    asr_input = json.dumps(
        [
            {"spk": "S01", "start": 0.0, "end": 2.5, "text": "helo wrold"},
            {"spk": "S01", "start": 2.8, "end": 6.1, "text": "this is a mesage form the suport team"},
            {"spk": "S02", "start": 6.5, "end": 9.0, "text": "ok tank you very mutch"},
        ],
        ensure_ascii=False,
    )
    fix_msgs = [
        {
            "role": "system",
            "content": "你是ASR文本校对器。用户给出一个JSON数组，每个元素含 spk/start/end/text 字段。请只修正 text 中的拼写和语法错误，spk/start/end 字段原样保留。只输出修复后的同结构JSON数组，不要输出任何其他文字或解释。",
        },
        {"role": "user", "content": "/no_think\n" + asr_input},
    ]
    out, *_ = run_chat("B-json-fix", fix_msgs, max_tokens=512)

    # 校验 JSON
    print("=" * 60)
    ok = False
    try:
        cleaned = out.strip()
        # 去掉可能的 markdown 代码围栏
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned
            cleaned = cleaned.rsplit("```", 1)[0].strip()
        data = json.loads(cleaned)
        assert isinstance(data, list) and len(data) == 3, "应为3个元素的数组"
        for i, (o, ref) in enumerate(zip(data, json.loads(asr_input))):
            assert o.get("spk") == ref["spk"], f"[{i}] spk 变了: {o.get('spk')}"
            assert o.get("start") == ref["start"], f"[{i}] start 变了: {o.get('start')}"
            assert o.get("end") == ref["end"], f"[{i}] end 变了: {o.get('end')}"
        print("[B] JSON 合法, spk/start/end 全部不变")
        print("[B] 修复后 text:", [o.get("text") for o in data])
        ok = True
    except Exception as e:
        print("[B] JSON 校验失败:", repr(e))
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
