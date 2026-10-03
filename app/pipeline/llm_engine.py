# -*- coding: utf-8 -*-
"""LLM 引擎：llama.cpp llama-server 进程管理 + OpenAI 兼容客户端。"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

import requests

from app.core.config import AppConfig
from app.pipeline.base import ProgressCallback, report_progress

# Windows 下隐藏控制台窗口
_CREATION_FLAGS = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def resolve_llama_server_exe() -> str:
    """解析 llama-server 可执行文件路径，找不到返回空串。

    查找顺序：
    - Windows: 内置 tools/llama.cpp/llama-server.exe；
    - macOS/Linux: 内置 tools/llama.cpp/llama-server → PATH（如 brew 安装）。
    """
    root = AppConfig.APP_ROOT / "tools" / "llama.cpp"
    if os.name == "nt":
        candidate = root / "llama-server.exe"
        if candidate.is_file():
            return str(candidate)
        return ""
    candidate = root / "llama-server"
    if candidate.is_file():
        return str(candidate)
    which = shutil.which("llama-server")
    return which or ""


def _free_port(base: int = 8817, tries: int = 10) -> int:
    """从 base 起找一个可绑定的空闲端口。"""
    import socket

    for port in range(base, base + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return base


def has_cuda_backend(server_exe: str) -> bool:
    """判断 llama-server 是否为 CUDA 构建（同目录存在 ggml-cuda.dll）。

    CPU-only 构建传 -ngl 无效，先探测再决定是否请求 GPU offload。
    """
    return bool(server_exe) and Path(server_exe).with_name("ggml-cuda.dll").is_file()


def cuda_backend_loadable(server_exe: str) -> bool:
    """探测 ggml-cuda.dll 是否可加载（依赖缺失时 llama.cpp 会静默回退 CPU）。

    ggml-cuda.dll 动态链接 cublas64_12.dll / cudart64_12.dll（官方 cudart
    伴随包提供）；缺失时 llama-server 照常启动、-ngl 不生效、全程 CPU，
    且不报任何错误。这里显式加载一次验证依赖完整（探测完立即卸载）。
    """
    if os.name != "nt":
        return True  # 非 Windows 构建后端内嵌，无需探测
    dll = Path(server_exe).with_name("ggml-cuda.dll")
    if not dll.is_file():
        return False
    import ctypes

    try:
        handle = ctypes.WinDLL(str(dll))
    except OSError:
        return False
    try:
        ctypes.windll.kernel32.FreeLibrary(handle._handle)
    except Exception:
        pass
    return True


def _build_server_cmd(
    exe: str, model_path: str, port: int, threads: int, use_gpu: bool
) -> list:
    """构造 llama-server 启动命令。

    GPU offload：-ngl 999 = 全部层进显存（llama.cpp CUDA 构建默认 ngl=0，
    不传该参数则完全跑 CPU）；CPU 构建（如 macOS brew/Metal 构建）忽略。
    --jinja：使用模型内嵌聊天模板（Qwen3.5 系需配合请求中的
    chat_template_kwargs.enable_thinking=False 关闭思考模式）。
    """
    cmd = [
        exe,
        "-m", str(model_path),
        "--port", str(port),
        "--host", "127.0.0.1",
        "-c", "8192",
        "-t", str(threads),
        "--jinja",
    ]
    if use_gpu:
        cmd += ["-ngl", "999"]
    return cmd


class LlamaServerManager:
    """llama-server.exe 后台进程管理器（支持上下文管理器）。

    用法::

        with LlamaServerManager() as server:
            server.start(model_path, port=8817, threads=8)
            client = LLMClient("http://127.0.0.1:8817")
            ...  # client.chat(...)
        # 退出 with 时自动 stop()
    """

    def __init__(self, server_exe: Optional[str] = None) -> None:
        if server_exe is None:
            server_exe = resolve_llama_server_exe()
        self._exe = server_exe
        self._proc: Optional[subprocess.Popen] = None
        self._log_file = None
        self._port = 8817

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    @property
    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(
        self,
        model_path: str,
        port: Optional[int] = None,
        threads: int = 8,
        device: str = "cpu",
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        """后台启动 llama-server 并轮询 /health 直到就绪（超时 120 秒）。

        device == "cuda" 时优先 GPU：CUDA 构建追加 -ngl 999（全部层 offload
        到显存）；-ngl 启动失败（驱动/显存问题）自动降级 CPU 重试一次。
        """
        if self.is_running:
            raise RuntimeError("llama-server 已在运行，请先调用 stop()")
        if not self._exe or not Path(self._exe).is_file():
            raise FileNotFoundError(
                "未找到 llama-server 可执行文件（Windows 使用内置 "
                "tools/llama.cpp/llama-server.exe；macOS/Linux 可通过 "
                "brew install llama.cpp 安装或放入 tools/llama.cpp/）"
            )
        if not Path(model_path).is_file():
            raise FileNotFoundError(f"未找到 LLM 模型: {model_path}")

        if port is None:
            port = _free_port()
        self._port = port

        use_gpu = device == "cuda" and has_cuda_backend(self._exe)
        if use_gpu and not cuda_backend_loadable(self._exe):
            use_gpu = False
            report_progress(
                progress, 0.0,
                "ggml-cuda.dll 依赖缺失（缺 cublas/cudart 运行库，可重新运行"
                "环境搭建补齐），llama-server 回退 CPU 推理",
            )
        elif device == "cuda":
            report_progress(
                progress, 0.0,
                "llama-server 启用 GPU 加速（-ngl 999）" if use_gpu
                else "llama-server 为 CPU 构建（缺少 ggml-cuda.dll），校对/翻译仅用 CPU",
            )
        cmd_cpu = _build_server_cmd(self._exe, model_path, port, threads, use_gpu=False)
        cmd_gpu = _build_server_cmd(self._exe, model_path, port, threads, use_gpu=True)
        try:
            self._launch(cmd_gpu if use_gpu else cmd_cpu, progress)
        except RuntimeError as exc:
            if not use_gpu:
                raise
            # GPU offload 启动失败（CUDA 不可用 / 显存装不下全部层）：降级 CPU
            report_progress(progress, 0.0, f"GPU offload 启动失败，降级 CPU 重试（{exc}）")
            self.stop()
            self._launch(cmd_cpu, progress)

    def _launch(self, cmd: list, progress: Optional[ProgressCallback]) -> None:
        """按给定命令启动进程并轮询 /health 直到就绪；进程退出抛 RuntimeError。"""
        report_progress(progress, 0.0, "启动 llama-server")
        # stderr 落临时文件，避免管道阻塞，失败时可读取诊断
        self._log_file = tempfile.TemporaryFile()
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file,
            stdin=subprocess.DEVNULL,
            creationflags=_CREATION_FLAGS,
        )

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"llama-server 启动失败（退出码 {self._proc.returncode}）:\n"
                    + self._read_log_tail()
                )
            try:
                r = requests.get(self.base_url + "/health", timeout=2)
                if r.status_code == 200 and r.json().get("status") == "ok":
                    report_progress(progress, 1.0, "llama-server 就绪")
                    return
            except (requests.RequestException, ValueError):
                pass
            time.sleep(1.0)
        self.stop()
        raise TimeoutError(f"llama-server /health 120 秒内未就绪:\n{self._read_log_tail()}")

    def _read_log_tail(self, max_chars: int = 2000) -> str:
        if self._log_file is None:
            return ""
        try:
            self._log_file.seek(0)
            data = self._log_file.read()
            return data.decode("utf-8", errors="replace")[-max_chars:]
        except OSError:
            return ""

    def kill(self) -> None:
        """立即杀死 llama-server 进程（不等待、不回收句柄）。

        供用户取消流水线时从 UI 线程调用：让 runner 线程中阻塞的 LLM
        HTTP 请求（单批次 CPU 推理可达数分钟）立刻失败，使取消即时
        生效。进程句柄与日志文件仍由 stop() 负责回收。
        """
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass

    def stop(self) -> None:
        """优雅终止 llama-server：terminate -> wait -> kill。"""
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
            self._log_file = None

    def __enter__(self) -> "LlamaServerManager":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


class LLMClient:
    """llama-server OpenAI 兼容 /v1/chat/completions 客户端（支持上下文管理器）。"""

    def __init__(self, base_url: str = "http://127.0.0.1:8817", timeout: float = 600.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def chat(
        self,
        system: str,
        user: str,
        temperature: float = 0.0,
        max_tokens: int = 4096,
    ) -> str:
        """单轮对话，返回助手回复文本。

        system 为空串时不发送 system 消息（Index 翻译模型只需 user 指令）；
        经 chat_template_kwargs.enable_thinking=False 关闭思考模式（模板
        预闭合 think 块，直接输出答案）；翻译官方推荐贪婪解码（temperature=0）。
        """
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": user.rstrip()})
        payload = {
            "model": "default",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
            "stream": False,
        }
        r = requests.post(
            self._base_url + "/v1/chat/completions",
            json=payload,
            timeout=self._timeout,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None
