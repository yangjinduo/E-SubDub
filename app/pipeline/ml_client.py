# -*- coding: utf-8 -*-
"""ML 推理引擎：demucs 人声分离 + MOSS ASR 的子进程管理（照 TTSEngine 模式）。

父进程（本模块）只管 worker（app/pipeline/ml_worker.py）生命周期与
JSON Lines 协议收发，不做任何 torch 推理：

- worker 解释器：冻结态（Nuitka 安装包）用 .venv-ml 的 Python（随安装
  包部署的独立虚拟环境，内含 torch + transformers 5.x + demucs）；
  开发态直接用当前解释器（主环境本就有全套依赖，dev 体验与旧版一致）。
- 之所以拆独立 venv：TTS 依赖 transformers 4.x，ASR（MOSS remote code）
  依赖 5.x，两者不能共存；.venv-tts 经 .pth 借 .venv-ml 的 torch/numpy
  并用自装的 transformers 4.52 遮蔽 5.x（见 env_setup.py）。
- 取消/异常时由调用方 shutdown() 结束 worker（显存随进程退出释放）；
  等待循环周期性泵 worker 日志并转发 progress——runner 的进度回调
  承担取消检查点，取消可在推理中途即时生效。

ASR/demucs 的报错分类（kind=memory 表示内存/显存不足，调用方不再用
换设备重试放大问题）与 TTS 一致。
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
from collections import deque
from pathlib import Path
from typing import Optional

from app.core.config import AppConfig, is_frozen, sanitized_subprocess_env
from app.core.ffmpeg import get_ffmpeg_path
from app.pipeline.base import ProgressCallback, report_progress

logger = logging.getLogger(__name__)

# ML 虚拟环境（冻结态 worker 解释器所在；dev 态仅作环境搭建目标存在）
ML_VENV_DIR = AppConfig.DATA_ROOT / ".venv-ml"

# worker 脚本：开发态为源码文件；冻结态为随包分发的数据文件
# （Nuitka 编译后 __file__ 指向虚拟路径，需回退到可执行文件同级的分发目录）
_WORKER_NAME = "ml_worker.py"


def _worker_script() -> Path:
    """定位 ml_worker.py 脚本磁盘路径。"""
    candidate = Path(__file__).with_name(_WORKER_NAME)
    if candidate.is_file():
        return candidate
    # 冻结态：脚本随包分发在 <exe 目录>/app/pipeline/ 下
    frozen_candidate = (
        Path(sys.executable).resolve().parent / "app" / "pipeline" / _WORKER_NAME
    )
    if frozen_candidate.is_file():
        return frozen_candidate
    raise FileNotFoundError(
        f"找不到 ml_worker.py（尝试过 {candidate} 与 {frozen_candidate}）"
    )


def get_ml_venv_python() -> Path:
    """返回 .venv-ml 的 Python 可执行文件路径。"""
    if os.name == "nt":
        return ML_VENV_DIR / "Scripts" / "python.exe"
    return ML_VENV_DIR / "bin" / "python"


def _ml_venv_site_packages() -> Path:
    """定位 .venv-ml 的 site-packages 目录（跨平台）。"""
    if os.name == "nt":
        return ML_VENV_DIR / "Lib" / "site-packages"
    lib = ML_VENV_DIR / "lib"
    for p in lib.glob("python*/site-packages"):
        if p.is_dir():
            return p
    return lib / "site-packages"


def get_ml_worker_python() -> Path:
    """worker 解释器：冻结态=.venv-ml；开发态=当前解释器。

    开发态主环境本就有 torch/demucs/transformers 5.x，直接用
    sys.executable 运行 worker 与旧版行为完全一致（.venv-ml 即使
    存在也不必用）；冻结态主程序无 Python 运行时，必须走 .venv-ml。
    """
    if is_frozen():
        py = get_ml_venv_python()
        if not py.is_file():
            raise RuntimeError(
                "缺少 ML 推理运行环境 .venv-ml（未创建或未完成依赖安装），"
                "请先在 设置 → 运行环境 中完成环境搭建"
            )
        return py
    return Path(sys.executable)


def ml_runtime_ready() -> tuple[bool, str]:
    """检查 ML 推理环境是否齐备，返回 (就绪, 缺失说明中文)。

    开发态：当前解释器能导入 torch/demucs/transformers 5.x 即就绪；
    冻结态：.venv-ml 存在且装有 torch/demucs/transformers 5.x/silero-vad。
    """
    if not is_frozen():
        try:
            import demucs  # noqa: F401
            import torch  # noqa: F401
            import transformers
        except Exception as exc:
            return False, f"当前环境缺少 ML 推理依赖（torch/demucs/transformers 5.x）：{exc}"
        major = int(transformers.__version__.split(".")[0])
        if major < 5:
            return False, f"ASR 需要 transformers 5.x（当前 {transformers.__version__}）"
        return True, ""

    py = get_ml_venv_python()
    if not py.is_file():
        return False, "缺少 ML 推理运行环境 .venv-ml（未创建或未完成依赖安装）"
    site_packages = _ml_venv_site_packages()
    for pkg in ("torch", "demucs", "transformers", "silero_vad"):
        if not (site_packages / pkg).is_dir():
            return False, f".venv-ml 内缺少依赖：{pkg}（需在运行环境中重新搭建）"
    return True, ""


class MLLoadMemoryError(RuntimeError):
    """内存/显存不足导致 ML 加载或推理失败（换 CPU 重试只会更糟，直接失败）。"""


class MLEngine:
    """ML 推理引擎：启动 / 请求 / 释放 worker 子进程。

    一个 worker 同时服务 demucs 分离与 MOSS ASR 两类请求（顺序使用，
    与流水线阶段顺序一致）；demucs 模型小按需加载不常驻，MOSS 由
    asr_load 显式加载、asr_unload 释放（转写大模型占显存，须在进入
    下一个阶段前让出）。
    """

    def __init__(self) -> None:
        self._proc: subprocess.Popen | None = None
        self._stdout_queue: queue.Queue = queue.Queue()
        self._stderr_lines: deque = deque()
        self._stderr_tail: deque = deque(maxlen=30)
        self._next_id = 1
        self._last_progress: tuple[float, str] | None = None

    @property
    def loaded(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    # ------------------------------------------------------------------ 启动

    def start(self, progress: Optional[ProgressCallback] = None) -> None:
        """启动 worker 并等待 ready 消息。"""
        self.shutdown()

        ffmpeg = get_ffmpeg_path()
        if not ffmpeg:
            raise RuntimeError("未找到内置 ffmpeg（libs/windows/ffmpeg.exe）")

        python = get_ml_worker_python()
        cmd = [str(python), str(_worker_script()), "--ffmpeg", str(ffmpeg)]

        report_progress(progress, 0.02, f"启动 ML 推理子进程（{python.parent.parent.name}）")
        # 净化环境：冻结态把随包 DLL 目录从 PATH 剔除，避免 worker 的
        # torch 原生扩展命中随包 DLL（WinError 1114 等）
        env = sanitized_subprocess_env()
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            cwd=str(AppConfig.DATA_ROOT),
            env=env,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        self._stdout_queue = queue.Queue()
        self._stderr_lines.clear()
        threading.Thread(target=self._drain_stdout, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()

        # 等待 ready（worker 启动即回，无重负载；异常时随异常消息快速失败）
        while True:
            msg = self._wait_message(timeout=0.5, progress=None)
            if msg is None:
                self._pump_stderr_logs(progress, frac=0.05)
                continue
            if msg.get("event") == "ready":
                break
            # 其余消息（理论上不会有）忽略继续等

    def shutdown(self) -> None:
        """结束 worker 子进程（先礼后兵），释放显存。"""
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None:
                try:
                    proc.stdin.write('{"cmd": "shutdown"}\n')  # type: ignore[union-attr]
                    proc.stdin.flush()  # type: ignore[union-attr]
                    proc.wait(timeout=5)
                except (OSError, subprocess.TimeoutExpired):
                    proc.kill()
                    proc.wait(timeout=5)
        finally:
            for stream in (proc.stdin, proc.stdout, proc.stderr):
                try:
                    if stream is not None:
                        stream.close()
                except OSError:
                    pass

    # ------------------------------------------------------------------ 请求

    def separate(
        self,
        full_audio_wav: str,
        work_dir: str,
        device: str = "cpu",
        progress: Optional[ProgressCallback] = None,
    ) -> tuple[str, str]:
        """人声分离请求，返回 (vocals_path, no_vocals_path)。"""
        rid = self._next_id
        self._next_id += 1
        self._send_request({
            "cmd": "separate", "id": rid,
            "audio": str(full_audio_wav), "work_dir": str(work_dir),
            "device": device,
        })
        msg = self._wait_result(rid, progress)
        return str(msg["vocals"]), str(msg["no_vocals"])

    def asr_load(
        self,
        model_dir: str,
        device: str = "cpu",
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        """加载 MOSS 转写模型到 worker（bf16 + sdpa + trust_remote_code）。"""
        rid = self._next_id
        self._next_id += 1
        self._send_request({
            "cmd": "asr_load", "id": rid,
            "model_dir": str(model_dir), "device": device,
        })
        self._wait_result(rid, progress)

    def asr_unload(self) -> None:
        """释放 worker 内的 MOSS 模型（显存让给后续阶段）。"""
        if not self.loaded:
            return
        rid = self._next_id
        self._next_id += 1
        self._send_request({"cmd": "asr_unload", "id": rid})
        self._wait_result(rid, None)

    def asr_transcribe(
        self,
        wav_path: str,
        chunk_minutes: int = 8,
        progress: Optional[ProgressCallback] = None,
    ) -> list[dict]:
        """转写请求，返回 [{"spk", "start", "end", "text"}, …]。"""
        rid = self._next_id
        self._next_id += 1
        self._send_request({
            "cmd": "asr_transcribe", "id": rid,
            "wav": str(wav_path), "chunk_minutes": int(chunk_minutes),
        })
        msg = self._wait_result(rid, progress)
        segments = []
        for spk, start, end, text in msg.get("segments", []):
            segments.append({
                "spk": str(spk), "start": float(start),
                "end": float(end), "text": str(text),
            })
        return segments

    # ------------------------------------------------------------------ 协议

    def _send_request(self, req: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise RuntimeError("ML worker 未启动")
        proc.stdin.write(json.dumps(req, ensure_ascii=False) + "\n")
        proc.stdin.flush()

    def _wait_result(
        self, rid: int, progress: Optional[ProgressCallback],
    ) -> dict:
        """等待指定 id 的完成/失败响应；期间泵进度与 worker 日志。

        等待循环每 0.5 秒泵一次 worker 日志（转发为 progress，消息固定
        避免刷屏）——runner 的进度回调承担取消检查点，因此取消可在
        推理中途即时生效（异常上抛后由调用方 shutdown 释放 worker）。
        """
        while True:
            msg = self._wait_message(timeout=0.5, progress=progress)
            if msg is None:
                # 心跳：泵日志并承担取消检查点（无进度事件的长块推理期）
                self._pump_stderr_logs(progress, frac=0.5)
                if self._last_progress is not None and progress is not None:
                    frac, desc = self._last_progress
                    report_progress(progress, frac, desc)
                continue
            if msg.get("event") == "progress":
                if msg.get("id") == rid:
                    frac = float(msg.get("frac") or 0.0)
                    desc = str(msg.get("desc") or "")
                    self._last_progress = (frac, desc)
                    report_progress(progress, frac, desc)
                continue
            if msg.get("id") != rid:
                continue  # 迟到的其他响应，忽略
            if not msg.get("ok"):
                error = str(msg.get("error") or "未知错误")
                if msg.get("kind") == "memory":
                    raise MLLoadMemoryError(f"内存/显存不足：ML 推理失败。{error}")
                raise RuntimeError(f"ML 推理失败：{error}")
            return msg

    # ------------------------------------------------------------------ 子进程 IO

    def _drain_stdout(self) -> None:
        """后台线程：stdout → 消息队列；EOF 放入空串哨兵。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        for line in iter(proc.stdout.readline, ""):
            line = line.strip()
            if line:
                self._stdout_queue.put(line)
        self._stdout_queue.put("")  # EOF 哨兵

    def _drain_stderr(self) -> None:
        """后台线程：stderr → 日志行缓冲（供泵出到界面/日志）。"""
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        for line in iter(proc.stderr.readline, ""):
            self._stderr_lines.append(line)

    def _wait_message(
        self, timeout: float, progress: Optional[ProgressCallback],
    ) -> Optional[dict]:
        """取下一条协议消息；超时返回 None；子进程退出抛 RuntimeError。"""
        try:
            line = self._stdout_queue.get(timeout=timeout)
        except queue.Empty:
            proc = self._proc
            if proc is not None and proc.poll() is not None:
                raise RuntimeError(
                    f"ML 子进程意外退出（code={proc.poll()}）：{self._stderr_text()}"
                )
            return None
        if line == "":
            raise RuntimeError(f"ML 子进程已退出：{self._stderr_text()}")
        try:
            return json.loads(line)
        except json.JSONDecodeError:
            logger.warning("ML worker 非协议消息: %s", line[:200])
            return None

    def _pump_stderr_logs(
        self,
        progress: Optional[ProgressCallback],
        frac: float = 0.5,
    ) -> None:
        """把 worker 的 stderr 日志行泵出到进度回调（或本地日志）。"""
        while True:
            try:
                line = self._stderr_lines.popleft()
            except IndexError:
                break
            line = line.strip()
            if not line:
                continue
            self._stderr_tail.append(line)
            if progress is not None:
                report_progress(progress, frac, line)
            else:
                logger.info("[ml-worker] %s", line)

    def _stderr_text(self) -> str:
        """报错用的 stderr 末尾若干行（截断到单行 200 字符）。"""
        lines = [ln[:200] for ln in self._stderr_tail]
        return " | ".join(lines[-8:]) or "（无输出）"
