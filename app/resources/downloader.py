# -*- coding: utf-8 -*-
"""模型下载器：纯 HTTP 直连魔搭 / hf-mirror，每资源一个 QThread 可并行。

为什么不使用 modelscope 客户端包：
- 冻结态（Nuitka 安装包）主程序显式排除了 torch，而 ``modelscope`` 的
  包初始化会触发 torch 导入——运行时直接 ImportError（"Module 'torch'
  was actively excluded from Nuitka compilation"）。因此下载不再依赖
  modelscope 包，改为直接调用其公开 HTTP API（普通 requests 即可）：
    列表：GET {api}/models/{repo}/repo/files?Revision=master&Recursive=true
    文件：GET {api}/models/{repo}/repo?Revision=master&FilePath={path}
- Index 系 GGUF（translate / homura）：官方未在魔搭发布（IndexTeam
  两 GGUF 仓库在魔搭为空仓库），走 HF 源——主仓库为社区量化仓库
  （mradermacher / Abu-Dju，文件布局与官方实测一致），域名级
  hf-mirror.com 优先、huggingface.co 直连兜底；IndexTeam 官方仓库
  经 fallback_repo_id 作跨仓库兜底：
    HF 列表：GET {base}/api/models/{repo}/tree/main?recursive=true
             （大仓库经响应 Link 头 rel="next" 翻页）
    HF 文件：GET {base}/{repo}/resolve/main/{path}
- 仓库级兜底（ResourceMeta.fallback_repo_id / fallback_patterns /
  fallback_source）：主仓库任一环节失效（不可达 / 缺文件）时整体切换
  兜底仓库重跑，同一目标目录——已完成的同名文件按续传跳过，不同名
  的半成品残件由 _finish 统一清扫。与域名级兜底叠加为三层容错：
  逐 URL 重试 → 同源域名轮换 → 跨仓库 / 跨源整体切换。

断点续传（文件级粒度）：
- 本地已存在且大小与远端一致的文件直接跳过；
- 下载先写为 <文件名>.incomplete，完成后 os.replace 原子改名；
- 取消 / 失败保留现场（含 .incomplete），重跑时已完成文件跳过、
  未完成文件重下——与就绪检测的 *.incomplete 语义一致（存在半成品
  即视为未就绪）。

进度：远端列表自带各文件字节数，总进度 = 已完成文件累计字节 +
当前文件已写字节（单文件下载中实时更新，数 GB 大文件不再整文件
无进度反馈）；速度按发射窗口内的已写字节计算，覆盖"跳过文件"的
续传场景。

全过程（开始 / 文件列表与续传基线 / 每文件起止 / 大文件 60 秒进度
里程碑 / 重试 / 失败堆栈）写入用户配置目录的部署日志 deploy日期.log
（与 startup 日志同目录，见 app/resources/deploy_log.py），失败的
弹窗信息附带该日志路径。
"""

from __future__ import annotations

import fnmatch
import logging
import os
import time
from pathlib import Path, PurePosixPath

import requests
from PySide6.QtCore import QObject, QThread, Signal

from app.core.i18n import tr
from app.resources.deploy_log import deploy_logger, deploy_log_path
from app.resources.model import RESOURCE_META, get_resource_manager

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 15  # 连接超时（秒）
_READ_TIMEOUT_S = 60  # 相邻数据块之间的读超时（秒）
_RETRIES = 3  # 单文件瞬时错误（网络抖动 / SSL / 5xx）重试次数
_RETRY_BACKOFF_S = 2.0  # 重试基础退避时长，逐次翻倍
_PROGRESS_INTERVAL_S = 0.5  # 进度信号最小发射间隔
_HF_MAX_PAGES = 20  # HF 列表翻页上限（防异常响应死循环）

_HEADERS = {"User-Agent": "ESubDub/0.1"}
_MS_API = "https://modelscope.cn/api/v1"
_HF_BASES = ("https://hf-mirror.com", "https://huggingface.co")


def _is_transient(exc: Exception) -> bool:
    """是否为可重试的瞬时错误（连接 / 超时 / SSL / 5xx；4xx 快速失败）。"""
    if isinstance(
        exc,
        (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
         requests.exceptions.SSLError),
    ):
        return True
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        return resp is None or resp.status_code >= 500
    return False


def _ms_list_files(repo_id: str) -> list[tuple[str, int]]:
    """魔搭仓库文件列表：[(相对路径, 字节数)，…]，目录（Type=tree）跳过。"""
    url = f"{_MS_API}/models/{repo_id}/repo/files"
    resp = requests.get(
        url,
        params={"Revision": "master", "Recursive": "true"},
        headers=_HEADERS,
        timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
    )
    resp.raise_for_status()
    data = (resp.json() or {}).get("Data") or {}
    files: list[tuple[str, int]] = []
    for item in data.get("Files") or []:
        if str(item.get("Type", "")) == "tree":
            continue
        path = str(item.get("Path") or "").strip()
        if path:
            files.append((path, int(item.get("Size") or 0)))
    return files


def _hf_next_page(resp: requests.Response) -> str | None:
    """解析列表响应 Link 头中的下一页 URL（rel="next"），无则 None。"""
    for part in resp.headers.get("Link", "").split(","):
        seg = [s.strip() for s in part.split(";")]
        if len(seg) < 2:
            continue
        if any(s == 'rel="next"' for s in seg[1:]):
            return seg[0].strip("<>")
    return None


def _hf_list_files(repo_id: str) -> list[tuple[str, int]]:
    """HF 仓库文件列表（hf-mirror 优先，huggingface.co 兜底）。"""
    dlog = deploy_logger()
    last_err: Exception | None = None
    for base in _HF_BASES:
        try:
            files: list[tuple[str, int]] = []
            next_url: str | None = f"{base}/api/models/{repo_id}/tree/main"
            pages = 0
            while next_url is not None and pages < _HF_MAX_PAGES:
                resp = requests.get(
                    next_url,
                    params={"recursive": "true"},
                    headers=_HEADERS,
                    timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
                )
                resp.raise_for_status()
                page = resp.json()
                if not isinstance(page, list):
                    raise ValueError(f"意外的列表响应结构：{str(page)[:200]}")
                for item in page:
                    if item.get("type") == "file" and item.get("path"):
                        files.append((str(item["path"]), int(item.get("size") or 0)))
                next_url = _hf_next_page(resp)
                pages += 1
            if files:
                return files
        except Exception as e:  # noqa: BLE001 - 换镜像源前记录上一错误
            last_err = e
            # 部署日志同步记录：云主机常见 hf-mirror 不可达 / huggingface.co
            # 被 SSL 中断，仅靠 root 日志排障时缺这条会误判为 URL 拼错
            dlog.warning("获取 HF 文件列表失败（%s），换下一个源重试：%s", base, e)
            logger.warning("获取 HF 文件列表失败（%s），换下一个源重试: %s", base, e)
    raise RuntimeError(f"无法获取 HuggingFace 仓库 {repo_id} 的文件列表：{last_err}")


def _list_remote_files(source: str, repo_id: str) -> list[tuple[str, int]]:
    """按来源拉取仓库文件列表。"""
    if source == "hf":
        return _hf_list_files(repo_id)
    return _ms_list_files(repo_id)


def _file_urls(source: str, repo_id: str, path: str) -> list[str]:
    """单个文件的下载 URL 候选列表（依次尝试）。"""
    quoted = requests.utils.quote(path)
    if source == "hf":
        return [f"{base}/{repo_id}/resolve/main/{quoted}" for base in _HF_BASES]
    return [
        f"{_MS_API}/models/{repo_id}/repo"
        f"?Revision=master&FilePath={requests.utils.quote(path)}"
    ]


def _match_patterns(rel_path: str, patterns: list[str] | None) -> bool:
    """allow_patterns 匹配：对相对路径或文件名任一命中即保留。"""
    if not patterns:
        return True
    name = PurePosixPath(rel_path).name
    return any(
        fnmatch.fnmatch(rel_path, p) or fnmatch.fnmatch(name, p)
        for p in patterns
    )


class _DownloadThread(QThread):
    """单个资源的后台下载线程（列表 → 过滤 → 逐文件下载，支持续传）。"""

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
        source: str,
        fallback_total_bytes: int,
        fallback_repo_id: str = "",
        fallback_patterns: list[str] | None = None,
        fallback_source: str = "",
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.key = key
        self.repo_id = repo_id
        self.target_dir = target_dir
        self.allow_patterns = allow_patterns
        self.source = source
        self.fallback_total_bytes = fallback_total_bytes
        # 仓库级兜底（见 run）：主仓库任一环节失败时整体切换重跑；
        # fallback_source 为空时与主源相同（魔搭↔HF 跨源兜底场景必填）
        self.fallback_repo_id = fallback_repo_id
        self.fallback_patterns = fallback_patterns
        self.fallback_source = fallback_source
        self._cancel = False
        # 部署日志（配置目录 deploy日期.log）：下载全程的过程级诊断
        self._dlog = deploy_logger()
        # 进度状态机（见 _emit_progress）：总进度 = 已完成文件累计字节
        # + 当前文件已写字节；速度窗口 = 发射间隔内的已写字节
        self._total_bytes = 1
        self._done_bytes = 0
        self._file_written = 0
        self._win_bytes = 0
        self._win_start = 0.0
        self._last_emit = 0.0

    def cancel(self) -> None:
        """请求取消：当前文件写完 .incomplete 后即停止（现场保留可续传）。"""
        self._cancel = True

    def _fail_text(self, error: str) -> str:
        """失败上报文案：错误信息 + 部署日志路径（弹窗中可直接定位日志）。"""
        path = deploy_log_path()
        if path is not None:
            return f"{error}\n{tr('common.log_hint', path=path)}"
        return error

    # ------------------------------------------------------------------ 线程体

    def run(self) -> None:
        dlog = self._dlog
        dlog.info(
            "=== 下载开始 [%s] ===（%s → %s，source=%s）",
            self.key, self.repo_id, self.target_dir, self.source,
        )
        # 下载源候选：主仓库 → 兜底仓库（可跨源：魔搭 ↔ HF）。主仓库
        # 任一环节失败（列表 / 模式匹配 / 文件下载）整体切换兜底仓库重
        # 跑：已完成的同名文件按续传跳过，不同名残件由 _finish 清扫。
        # 典型场景：国内云主机 HF 两域名均不可达 → 落到魔搭官方镜像；
        # 海外用户魔搭慢 → 落到 HF。
        candidates: list[tuple[str, str, list[str] | None]] = [
            (self.source, self.repo_id, self.allow_patterns),
        ]
        if self.fallback_repo_id:
            fb_patterns = (
                self.fallback_patterns
                if self.fallback_patterns is not None
                else self.allow_patterns
            )
            candidates.append(
                (self.fallback_source or self.source, self.fallback_repo_id, fb_patterns)
            )

        last_error = ""
        for idx, (source, repo_id, patterns) in enumerate(candidates):
            if idx > 0:
                dlog.warning(
                    "[%s] 上一仓库下载失败（%s），切换兜底仓库 %s（source=%s）",
                    self.key, last_error, repo_id, source,
                )
            try:
                self._download_repo(source, repo_id, patterns)
                return  # 成功：_finish 已完成上报
            except _CancelledError:
                self.cancelled.emit(self.key)  # 取消语境日志已在抛出前记录
                return
            except Exception as e:  # noqa: BLE001 - 换兜底仓库前记录错误
                if self._cancel:  # 取消与失败竞争时按取消收尾
                    self.cancelled.emit(self.key)
                    return
                last_error = f"{type(e).__name__}: {e}"
                dlog.error("[%s] 仓库 %s 下载失败：%s", self.key, repo_id, last_error)
        dlog.error(
            "=== 下载失败 [%s]：已尝试 %d 个仓库均失败 ===",
            self.key, len(candidates),
        )
        self.failed.emit(
            self.key,
            self._fail_text(
                f"下载失败（已尝试 {len(candidates)} 个仓库）：{last_error}"
            ),
        )

    def _download_repo(
        self, source: str, repo_id: str, patterns: list[str] | None
    ) -> None:
        """单仓库完整下载流程（列表 → 过滤 → 续传基线 → 逐文件下载）。

        source 指明本候选的下载源（"modelscope" / "hf"，主源与兜底源可
        不同）。成功（含续传直接完成）时经 _finish 完成上报后正常返回；
        用户取消抛 _CancelledError；其余任何失败抛 RuntimeError——由
        run() 决定是否换兜底仓库重试。
        """
        dlog = self._dlog
        try:
            files = _list_remote_files(source, repo_id)
        except Exception as e:  # noqa: BLE001 - 失败原因完整带入上层
            raise RuntimeError(f"获取仓库文件列表失败：{e}") from e

        files = [(p, s) for p, s in files if _match_patterns(p, patterns)]
        if not files:
            raise RuntimeError("仓库文件列表为空或均未匹配下载模式")

        total_bytes = sum(size for _, size in files)
        if total_bytes <= 0:
            total_bytes = self.fallback_total_bytes or 1

        # 续传基线：已完整存在的文件直接计入完成字节
        done_bytes = 0
        todo: list[tuple[str, int, int]] = []  # (rel_path, remote_size, local_size)
        for rel_path, size in files:
            local = self.target_dir / Path(*PurePosixPath(rel_path).parts)
            local_size = local.stat().st_size if local.is_file() else -1
            if local_size == size:
                done_bytes += size
            else:
                todo.append((rel_path, size, local_size))

        dlog.info(
            "[%s] 仓库 %s 文件列表 %d 项，合计 %.2f GB；续传跳过 %d 项，"
            "待下载 %d 项（%.2f GB）",
            self.key, repo_id, len(files), total_bytes / 1024**3,
            len(files) - len(todo), len(todo),
            sum(size for _, size, _ in todo) / 1024**3,
        )
        # 进度状态机（见 _emit_progress）：总进度 = 已完成文件累计字节 +
        # 当前文件已写字节——数 GB 单文件下载中途也能实时更新百分比
        self._total_bytes = total_bytes
        self._done_bytes = done_bytes
        self._file_written = 0
        self._reset_progress_window()
        self._emit_progress(force=True)
        if not todo:
            dlog.info("[%s] 全部文件已就位（续传跳过），直接完成", self.key)
            self._finish(total_bytes, done_bytes)
            return

        for rel_path, size, _local in todo:
            if self._cancel:
                dlog.info(
                    "=== 下载取消 [%s]：已完成 %d/%d 项 ===",
                    self.key, len(files) - len(todo), len(files),
                )
                raise _CancelledError()
            dlog.info(
                "[%s] 开始下载 %s（%.1f MB）", self.key, rel_path, size / 1024 / 1024
            )
            self._file_written = 0
            try:
                self._download_one(rel_path, size, repo_id, source)
            except _CancelledError:
                dlog.info("=== 下载取消 [%s]（%s 进行中取消）===", self.key, rel_path)
                raise
            except Exception as e:  # noqa: BLE001 - 失败原因完整带入上层
                logger.exception("下载 %s 的 %s 失败", self.key, rel_path)
                dlog.exception("=== 下载失败 [%s]：%s ===", self.key, rel_path)
                raise RuntimeError(
                    f"下载 {rel_path} 失败：{type(e).__name__}: {e}"
                ) from e
            self._done_bytes += size
            self._file_written = 0
            dlog.info(
                "[%s] 完成 %s（累计 %.2f/%.2f GB）",
                self.key, rel_path, self._done_bytes / 1024**3, total_bytes / 1024**3,
            )
            self._emit_progress()

        if self._cancel:
            dlog.info("=== 下载取消 [%s] ===", self.key)
            raise _CancelledError()
        self._finish(total_bytes, self._done_bytes)

    # ------------------------------------------------------------------ 进度

    def _reset_progress_window(self) -> None:
        """重置速度计算窗口（窗口内已写字节 + 窗口起点 + 上次发射时间）。"""
        self._win_bytes = 0
        self._win_start = time.monotonic()
        self._last_emit = self._win_start

    def _emit_progress(self, force: bool = False) -> None:
        """节流发射总进度（0-1）与速度（间隔不足时跳过，force 立即发射）。

        总进度 = 已完成文件累计字节 + 当前文件已写字节，覆盖单文件内部
        的实时进度（修复"数 GB 单文件下载中一直 0%"）；速度按窗口内
        已写字节计算，续传跳过阶段（无写入）速度为空串。
        """
        now = time.monotonic()
        if not force and now - self._last_emit < _PROGRESS_INTERVAL_S:
            return
        elapsed = max(now - self._win_start, 0.1)
        speed = (
            f"{self._win_bytes / elapsed / (1024 * 1024):.1f} MB/s"
            if self._win_bytes > 0 else ""
        )
        done = self._done_bytes + self._file_written
        self.progress.emit(self.key, min(0.99, done / self._total_bytes), speed)
        self._last_emit = now
        self._win_bytes = 0
        self._win_start = now

    # ------------------------------------------------------------------ 内部

    def _download_one(
        self, rel_path: str, remote_size: int, repo_id: str, source: str
    ) -> None:
        """下载单个文件：候选 URL 逐个尝试，瞬时错误退避重试。

        写入 <文件名>.incomplete，完成后 os.replace 原子改名；既有的
        错误尺寸残件（含历史 .incomplete）一并清掉重下。
        """
        dest = self.target_dir / Path(*PurePosixPath(rel_path).parts)
        tmp = dest.with_name(dest.name + ".incomplete")
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp.unlink(missing_ok=True)
        if dest.exists():
            dest.unlink()

        urls = _file_urls(source, repo_id, rel_path)
        last_err: Exception | None = None
        for attempt in range(1 + _RETRIES):
            if self._cancel:
                raise _CancelledError()
            for url in urls:
                try:
                    self._stream_to_file(url, tmp, remote_size)
                    os.replace(tmp, dest)
                    return
                except _CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - 记录后按类型决定重试
                    last_err = e
                    if not _is_transient(e):
                        self._dlog.error(
                            "[%s] 下载 %s 失败（不可重试错误）：%s", self.key, rel_path, e
                        )
                        raise
                    self._dlog.warning(
                        "[%s] 下载 %s（%s）失败，重试 %d/%d：%s",
                        self.key, rel_path, url, attempt + 1, 1 + _RETRIES, e,
                    )
                    logger.warning(
                        "下载 %s（%s）失败，重试 %d/%d：%s",
                        rel_path, url, attempt + 1, 1 + _RETRIES, e,
                    )
            time.sleep(_RETRY_BACKOFF_S * (attempt + 1))
        raise RuntimeError(f"下载 {rel_path} 失败（已重试 {_RETRIES} 次）：{last_err}")

    def _stream_to_file(self, url: str, tmp: Path, remote_size: int) -> None:
        """流式下载单个 URL 到 tmp（Content-Length 与已写字节实时校验）。"""
        last_log = time.monotonic()
        with requests.get(
            url, stream=True, headers=_HEADERS,
            timeout=(_CONNECT_TIMEOUT_S, _READ_TIMEOUT_S),
            allow_redirects=True,
        ) as resp:
            resp.raise_for_status()
            content_length = int(resp.headers.get("Content-Length", 0) or 0)
            written = 0
            with open(tmp, "wb") as fh:
                for chunk in resp.iter_content(chunk_size=1 << 20):
                    if self._cancel:
                        raise _CancelledError()
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
                    # 实时进度：当前文件已写字节计入总进度（见 _emit_progress）
                    self._win_bytes += len(chunk)
                    self._file_written = written
                    self._emit_progress()
                    # 部署日志进度里程碑：每 60 秒一行（进度信号不落盘），
                    # 大文件（数 GB 单文件）下载中断时据此还原现场进度
                    now = time.monotonic()
                    if now - last_log >= 60:
                        self._dlog.info(
                            "[%s] %s 已写入 %.0f MB / %.0f MB",
                            self.key, tmp.name, written / 1024 / 1024,
                            remote_size / 1024 / 1024,
                        )
                        last_log = now
        if remote_size > 0 and written != remote_size:
            raise RuntimeError(
                f"文件大小校验失败：期望 {remote_size} 字节，实际写入 {written} 字节"
            )
        if content_length > 0 and written != content_length:
            raise RuntimeError(
                f"传输不完整：Content-Length {content_length}，实际写入 {written} 字节"
            )

    def _finish(self, total_bytes: int, done_bytes: int) -> None:
        """全部文件就位后的收尾：清扫残留半成品 → 校验 → 成功上报。"""
        # 仓库结构变化时可能遗留不再需要的 .incomplete，一并清理
        for stale in self.target_dir.rglob("*.incomplete"):
            try:
                stale.unlink()
            except OSError:
                pass
        if done_bytes + 1 < total_bytes:  # 容差 1 字节防取整误差
            self._dlog.error(
                "=== 下载校验失败 [%s]：%.2f/%.2f GB ===",
                self.key, done_bytes / 1024**3, total_bytes / 1024**3,
            )
            self.failed.emit(self.key, self._fail_text("下载校验失败：部分文件缺失"))
            return
        self._dlog.info(
            "=== 下载完成 [%s]（%s，%.2f GB）===",
            self.key, self.target_dir, total_bytes / 1024**3,
        )
        self.progress.emit(self.key, 1.0, "")
        self.succeeded.emit(self.key, str(self.target_dir))


class _CancelledError(Exception):
    """用户主动取消（内部控制流用）。"""


class ModelDownloader(QObject):
    """模型下载器：纯 HTTP 下载到指定目录，多资源可并行（每资源一线程）。

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
        source: str | None = None,
        total_size_gb: float | None = None,
        fallback_repo_id: str | None = None,
        fallback_patterns: list[str] | None = None,
        fallback_source: str | None = None,
    ) -> None:
        """启动下载（在 QThread 中执行 HTTP 下载）。

        参数：
            key: 资源标识（同一 key 重复下载会被忽略并记录 warning）
            repo_id: 仓库 id（魔搭如 "openmoss/MOSS-Transcribe-Diarize"；
                HF 如 "Abu-Dju/Index-Homura-2B-Q8_0-GGUF"）
            target_dir: 下载目标目录（相对仓库根布局落盘）
            allow_patterns: 文件匹配模式（None 表示整仓下载）
            source: "modelscope" 或 "hf"（缺省时查 RESOURCE_META[key]）
            total_size_gb: 列表拿不到体积时的估算兜底（GB）
            fallback_repo_id: 兜底仓库 id（缺省时查 RESOURCE_META[key]；
                主仓库失效时下载器整体切换重跑）
            fallback_patterns: 兜底仓库的文件模式（缺省查元数据；仓库
                布局不同时必须显式给出）
            fallback_source: 兜底仓库的下载源（缺省查元数据；跨源兜底
                场景必填，如 主源魔搭 → 兜底 HF）

        下载完成后由接收方（设置窗口）刷新 ResourceManager 状态；
        若无人接管（窗口已关闭），_on_succeeded 按默认路径检测兜底刷新。
        """
        existing = self._threads.get(key)
        if existing is not None and existing.isRunning():
            logger.warning("资源 %s 已在下载中，忽略重复的下载请求", key)
            return

        meta = RESOURCE_META.get(key)
        if source is None or source == "":
            source = meta.source if meta else "modelscope"
        if total_size_gb is None:
            total_size_gb = meta.download_size_gb if meta else 0.0
        if fallback_repo_id is None:
            fallback_repo_id = meta.fallback_repo_id if meta else ""
        if fallback_patterns is None:
            fallback_patterns = meta.fallback_patterns if meta else None
        if fallback_source is None:
            fallback_source = meta.fallback_source if meta else ""

        thread = _DownloadThread(
            key, repo_id, Path(target_dir), allow_patterns, source,
            int(total_size_gb * 1024 ** 3),
            fallback_repo_id=fallback_repo_id,
            fallback_patterns=fallback_patterns,
            fallback_source=fallback_source,
        )
        thread.progress.connect(self.download_progress)
        thread.succeeded.connect(self._on_succeeded)
        thread.failed.connect(self.download_failed)
        thread.cancelled.connect(self.download_cancelled)
        # 线程结束后从登记表清理（QThread 内置 finished 信号）
        thread.finished.connect(lambda k=key: self._threads.pop(k, None))
        self._threads[key] = thread
        thread.start()

    def cancel(self, key: str) -> None:
        """取消指定资源的下载（当前文件写完 .incomplete 后停止，现场保留）。"""
        thread = self._threads.get(key)
        if thread is None or not thread.isRunning():
            logger.warning("资源 %s 当前没有正在进行的下载", key)
            return
        thread.cancel()
        logger.info("已请求取消资源 %s 的下载（已完成文件保留，可续传）", key)

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
        if any(info.key == key for info in manager.resources()):
            if not manager.get(key).status.is_ready:
                manager.refresh(key)


# 模块级单例：通过 get_model_downloader() 获取。
# 使用单例保证下载线程不随设置窗口关闭而被回收，可在后台继续下载。
_DOWNLOADER: ModelDownloader | None = None


def get_model_downloader() -> ModelDownloader:
    """获取 ModelDownloader 单例（首次调用时创建）。"""
    global _DOWNLOADER
    if _DOWNLOADER is None:
        _DOWNLOADER = ModelDownloader()
    return _DOWNLOADER
