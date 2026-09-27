# -*- coding: utf-8 -*-
"""IndexTTS-2.5 推理子进程 worker（运行于 .venv-tts 独立环境）。

IndexTTS-2.5 官方依赖 transformers 4.52，与主环境（ASR 所需 5.x）冲突，
故由 app/pipeline/tts.py 的 TTSEngine 用 .venv-tts 的 Python 启动本脚本。

协议（JSON Lines，一行一条）：
- 通道：stdin 收请求，原 stdout 回响应；第三方库的全部 stdout 输出在启动
  时重定向到 stderr（父进程读取后转发到界面日志），协议通道保持纯净；
- 启动参数：--model-dir <目录> --device <cuda|cpu> [--bf16]；
- 加载完成：{"event": "ready"}；加载失败：{"event": "fatal", "error": ...,
  "kind": "memory" | "error"}（kind=memory 时父进程不再尝试 CPU 重试）；
- 请求：{"id": int, "text": str, "ref": str, "out": str, "lang": str}
  响应：{"id": int, "ok": true, "duration": float}
        {"id": int, "ok": false, "error": str, "kind": ...}
  进度：{"id": int, "event": "progress", "frac": 0~1, "desc": str}（合成期间）
- {"cmd": "shutdown"} 或 stdin 关闭即退出；合成结果即时落盘，进程被 kill
  也不丢已完成句子。

路径约定（免 pip install，规避官方 requires-python <3.12 限制）：
- third_party/index-tts 加入 sys.path → 直接 import indextts 包；
- torchaudio 用 .venv-tts 内安装的 2.8.0（pip --no-deps 安装，且 C 扩展
  pyd/so 已剥离——它与共享的 torch 2.14 二进制不兼容，剥离后纯 Python 层
  的 transforms/functional/compliance.kaldi 照常工作，I/O 走 soundfile
  后端）；项目根仅 append 到 sys.path 末尾，其中的 torchaudio 极简替身
  只作未安装真包时的兜底，不得遮蔽真包。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# 项目根（app/pipeline/tts_worker.py → 上两级）；third_party 官方仓库
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_INDEX_REPO = _PROJECT_ROOT / "third_party" / "index-tts"
sys.path.insert(0, str(_INDEX_REPO))
# 项目根放末尾（不遮蔽 .venv-tts site-packages 里的真 torchaudio）
sys.path.append(str(_PROJECT_ROOT))


def _redirect_stdout_to_stderr() -> int:
    """fd 1 → fd 2，返回原 stdout 的副本 fd 供协议通道使用。

    IndexTTS / transformers 的加载与推理会向 stdout 打印 ">> ..." 等信息，
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


def main() -> int:
    parser = argparse.ArgumentParser(description="IndexTTS-2.5 TTS worker")
    parser.add_argument("--model-dir", required=True, help="IndexTTS-2.5 模型目录")
    parser.add_argument("--device", required=True, help="cuda / cuda:0 / cpu")
    parser.add_argument("--bf16", action="store_true", help="cuda 下启用 bf16")
    args = parser.parse_args()

    proto_fd = _redirect_stdout_to_stderr()
    proto = os.fdopen(proto_fd, "w", encoding="utf-8", buffering=1)
    stdin = sys.stdin

    def send(obj: dict) -> None:
        try:
            proto.write(json.dumps(obj, ensure_ascii=False) + "\n")
        except (BrokenPipeError, OSError):
            raise SystemExit(2)  # 父进程已退出

    # ---- 加载模型（一次）----
    try:
        from indextts.infer_v2_5 import IndexTTS2

        tts = IndexTTS2(
            cfg_path=str(Path(args.model_dir) / "config.yaml"),
            model_dir=str(args.model_dir),
            use_bf16=args.bf16,
            device=args.device,
        )
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 加载失败原样上报父进程
        send({
            "event": "fatal",
            "error": f"{type(exc).__name__}: {exc}",
            "kind": _classify(exc),
        })
        return 3

    send({"event": "ready"})

    # IndexTTS2 内置 gr_progress 阶段锚点（0 起步 / 0.1 文本处理 /
    # 0.2+0.7·i/n 逐段生成 / 0.9 落盘）→ 转发为协议进度事件（父进程
    # 在锚点间插值后供复核窗口显示百分比）
    _current = {"rid": None}

    def _progress(frac, desc=""):
        rid = _current["rid"]
        if rid is not None:
            send({
                "id": rid, "event": "progress",
                "frac": float(frac), "desc": str(desc or ""),
            })

    tts.gr_progress = _progress

    # ---- 请求循环 ----
    import soundfile as sf

    for line in iter(stdin.readline, ""):
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            continue
        if req.get("cmd") == "shutdown":
            break
        rid = req.get("id")
        _current["rid"] = rid  # gr_progress 锚点归入当前请求
        try:
            tts.infer(
                spk_audio_prompt=req["ref"],
                text=req["text"],
                lang=req.get("lang") or "zh",
                output_path=req["out"],
            )
            info = sf.info(req["out"])
            send({
                "id": rid, "ok": True,
                "duration": float(info.frames) / float(info.samplerate),
            })
        except BaseException as exc:  # noqa: BLE001 - 单句失败不终止 worker
            send({
                "id": rid, "ok": False,
                "error": f"{type(exc).__name__}: {exc}",
                "kind": _classify(exc),
            })
        finally:
            _current["rid"] = None
    return 0


if __name__ == "__main__":
    sys.exit(main())
