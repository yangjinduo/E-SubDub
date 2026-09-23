"""模型下载器：在 QThread 中执行魔搭 snapshot_download，并以轮询目录的方式估算进度。

进度方案说明：modelscope 的 snapshot_download 不暴露进度回调，这里在下载线程
启动前统计目标目录已有字节（支持断点续传时非 0），随后每 0.5s 递归扫描目标
目录（含 *.incomplete 等隐藏临时文件）统计已写字节，除以预估总大小
（RESOURCE_META 中声明的 download_size_gb）得到百分比，同时按相邻两次采样
的增量计算下载速度。

取消限制说明：snapshot_download 无法从外部中断，cancel() 只置取消标志；
线程会在当前文件写完、snapshot_download 自然返回后，把（可能不完整的）
目标目录移入 .trash 并以 download_failed 上报"已取消"，UI 回到未就绪态。
"""

from __future__ import annotations

import logging
import ssl
import threading
import time
from pathlib import Path

import requests
from PySide6.QtCore import QObject, QThread, Signal

from app.resources.model import (
    RESOURCE_META,
    dir_size_bytes,
    get_resource_manager,
    move_to_trash,
)

logger = logging.getLogger(__name__)

_POLL_INTERVAL_S = 0.5  # 进度轮询间隔（秒）
_SSL_RETRIES = 3  # SSL 错误自动重试次数（POC 时网络偶发 SSL 错误）
_SSL_RETRY_BACKOFF_S = 2.0  # 重试基础退避时长，逐次翻倍


class _DownloadThread(QThread):
    """执行 snapshot_download 的后台线程（含轮询进度、SSL 重试与取消处理）。"""

    progress = Signal(str, float, str)  # key, percent(0-1), speed_str
    succeeded = Signal(str, str)  # key, local_path
    failed = Signal(str, str)  # key, error
    cancelled = Signal(str)  # key（用户主动取消，区别于失败）

    def __init__(
        self,
        key: str,
        repo_id: str,
        target_dir: Path,
        allow_patterns: list[str] | None,
        total_bytes: int,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.repo_id = repo_id
        self.target_dir = target_dir
        self.allow_patterns = allow_patterns
        self.total_bytes = total_bytes
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        """置取消标志（见模块 docstring 中的限制说明）。"""
        self._cancel_event.set()

    # ------------------------------------------------------------------ 线程体

    def run(self) -> None:  # noqa: C901 - 分支多为错误处理
        self.target_dir.mkdir(parents=True, exist_ok=True)
        baseline = dir_size_bytes(self.target_dir)  # 已有字节（断点续传时非 0）

        # 轮询线程与下载并行：扫描期间 modelscope 导入（数秒）也能发出心跳进度
        stop = threading.Event()
        poller = threading.Thread(target=self._poll_loop, args=(baseline, stop), daemon=True)
        poller.start()

        error: Exception | None = None
        cancelled = False
        try:
            # modelscope 导入较慢，放在工作线程内，避免阻塞 UI 线程
            from modelscope import snapshot_download

            for attempt in range(1 + _SSL_RETRIES):
                if self._cancel_event.is_set():
                    cancelled = True
                    break
                try:
                    snapshot_download(
                        self.repo_id,
                        local_dir=str(self.target_dir),
                        allow_patterns=self.allow_patterns,
                    )
                    error = None
                    break
                except (ssl.SSLError, requests.exceptions.SSLError) as e:
                    error = e
                    logger.warning(
                        "下载 %s（%s）出现 SSL 错误，第 %d/%d 次重试: %s",
                        self.key, self.repo_id, attempt, _SSL_RETRIES, e,
                    )
                    time.sleep(_SSL_RETRY_BACKOFF_S * attempt)
        except Exception as e:  # 其余异常不重试，直接失败
            error = e
        finally:
            stop.set()
            poller.join(timeout=2)

        if cancelled or self._cancel_event.is_set():
            # 简化实现：等待 snapshot_download 返回后清理目录，见模块 docstring
            move_to_trash(self.target_dir)
            self.cancelled.emit(self.key)
            return

        if error is not None:
            # 半成品目录移入 .trash（而非直接删除），避免误删用户数据
            move_to_trash(self.target_dir)
            self.failed.emit(self.key, f"{type(error).__name__}: {error}")
            return

        self.progress.emit(self.key, 1.0, "")
        self.succeeded.emit(self.key, str(self.target_dir))

    # ------------------------------------------------------------------ 进度轮询

    def _poll_loop(self, baseline: int, stop: threading.Event) -> None:
        """每 0.5s 扫描目标目录，按预估总大小换算百分比与速度并发射进度。"""
        prev_bytes, prev_time = baseline, time.monotonic()
        while not stop.wait(_POLL_INTERVAL_S):
            current = dir_size_bytes(self.target_dir)
            now = time.monotonic()
            interval = now - prev_time
            speed = (current - prev_bytes) / interval if interval > 0 else 0.0
            if self.total_bytes > 0:
                percent = min(0.99, max(0.0, (current - baseline) / self.total_bytes))
            else:
                percent = 0.0
            self.progress.emit(self.key, percent, f"{speed / (1024 * 1024):.1f} MB/s")
            prev_bytes, prev_time = current, now


class ModelDownloader(QObject):
    """模型下载器：通过魔搭 snapshot_download 下载模型到指定目录。

    信号：
        download_progress(str, float, str): 进度，参数为资源 key、百分比（0-1）、
            速度字符串（如 "3.2 MB/s"）
        download_finished(str, str): 下载完成，参数为资源 key、本地目录
        download_failed(str, str): 下载失败，参数为资源 key、错误信息
        download_cancelled(str): 用户取消下载，参数为资源 key
    """

    download_progress = Signal(str, float, str)
    download_finished = Signal(str, str)
    download_failed = Signal(str, str)
    download_cancelled = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._threads: dict[str, _DownloadThread] = {}

    # ------------------------------------------------------------------ 控制

    def start_download(
        self,
        key: str,
        repo_id: str,
        target_dir: str,
        allow_patterns: list[str] | None = None,
        total_size_gb: float | None = None,
    ) -> None:
        """启动下载（在 QThread 中执行 snapshot_download）。

        参数：
            key: 资源标识（同一 key 重复下载会被忽略并记录 warning）
            repo_id: 魔搭仓库 id（如 "Qwen/Qwen3-4B-GGUF"）
            target_dir: 下载目标目录（支持用户自选位置）
            allow_patterns: 文件匹配模式（None 表示整仓下载）
            total_size_gb: 预估总大小（GB）；缺省时查 RESOURCE_META[key]

        下载到默认子目录并成功完成后，会自动刷新 ResourceManager 中对应
        资源的状态；下载到用户自选位置时由 UI 调用 update_status() 接管。
        """
        existing = self._threads.get(key)
        if existing is not None and existing.isRunning():
            logger.warning("资源 %s 已在下载中，忽略重复的下载请求", key)
            return

        if total_size_gb is None:
            meta = RESOURCE_META.get(key)
            total_size_gb = meta.download_size_gb if meta else 0.0
        total_bytes = int(total_size_gb * 1024 ** 3)

        thread = _DownloadThread(key, repo_id, Path(target_dir), allow_patterns, total_bytes)
        thread.progress.connect(self.download_progress)
        thread.succeeded.connect(self._on_succeeded)
        thread.failed.connect(self.download_failed)
        thread.cancelled.connect(self.download_cancelled)
        # 线程结束后从登记表清理（QThread 内置 finished 信号）
        thread.finished.connect(lambda k=key: self._threads.pop(k, None))
        self._threads[key] = thread
        thread.start()

    def cancel(self, key: str) -> None:
        """取消指定资源的下载（限制见模块 docstring：等待当前文件完成后清理）。"""
        thread = self._threads.get(key)
        if thread is None or not thread.isRunning():
            logger.warning("资源 %s 当前没有正在进行的下载", key)
            return
        thread.cancel()
        logger.info("已请求取消资源 %s 的下载（将在当前文件完成后清理）", key)

    def is_downloading(self, key: str) -> bool:
        """指定资源是否正在下载。"""
        thread = self._threads.get(key)
        return thread is not None and thread.isRunning()

    # ------------------------------------------------------------------ 内部

    def _on_succeeded(self, key: str, local_path: str) -> None:
        self.download_finished.emit(key, local_path)
        # download_finished 的接收方（如设置窗口）会先刷新状态；若无人接管
        # （如窗口已关闭），这里按默认路径检测兜底刷新，供资源卡片更新。
        manager = get_resource_manager()
        # codec 是 tts 的附属下载（无独立资源行），完成时刷新 tts 的就绪判定
        refresh_key = "tts" if key == "tts_codec" else key
        if any(info.key == refresh_key for info in manager.resources()):
            if not manager.get(refresh_key).status.is_ready:
                manager.refresh(refresh_key)


# 模块级单例：通过 get_model_downloader() 获取。
# 使用单例保证下载线程不随设置窗口关闭而被回收，可在后台继续下载。
_DOWNLOADER: ModelDownloader | None = None


def get_model_downloader() -> ModelDownloader:
    """获取 ModelDownloader 单例（首次调用时创建）。"""
    global _DOWNLOADER
    if _DOWNLOADER is None:
        _DOWNLOADER = ModelDownloader()
    return _DOWNLOADER
