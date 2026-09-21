# -*- coding: utf-8 -*-
"""LLM 引擎：llama.cpp llama-server 进程管理 + OpenAI 兼容客户端。"""

from __future__ import annotations

import os
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
            server_exe = str(AppConfig.APP_ROOT / "tools" / "llama.cpp" / "llama-server.exe")
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
        port: int = 8817,
        threads: int = 8,
        progress: Optional[ProgressCallback] = None,
    ) -> None:
        """后台启动 llama-server 并轮询 /health 直到就绪（超时 120 秒）。

        启动参数沿用 POC 结论：-m <model> --port <port> --host 127.0.0.1
        -c 8192 -t <threads>。
        """
        if self.is_running:
            raise RuntimeError("llama-server 已在运行，请先调用 stop()")
        if not Path(self._exe).is_file():
            raise FileNotFoundError(f"未找到 llama-server: {self._exe}")
        if not Path(model_path).is_file():
            raise FileNotFoundError(f"未找到 LLM 模型: {model_path}")

        self._port = port
        cmd = [
            self._exe,
            "-m", str(model_path),
            "--port", str(port),
            "--host", "127.0.0.1",
            "-c", "8192",
            "-t", str(threads),
        ]
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
        temperature: float = 0.2,
        max_tokens: int = 4096,
    ) -> str:
        """单轮对话，返回助手回复文本。

        自动在 user 末尾追加 " /no_think"（Qwen3 关闭思考模式）；
        temperature <= 0.3 时 JSON 遵循率最佳（POC 结论）。
        """
        payload = {
            "model": "default",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user.rstrip() + " /no_think"},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens,
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
