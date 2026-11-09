"""自动更新的 UI 编排：后台线程检查 / 下载，信号回主线程。

流程（2026-11 破局改造，两层）：
1. 首选自建传输层（app/core/updater_net.py：requests 走系统代理、
   连接/读超时真实生效、流式下载可随时取消——与浏览器同栈）：
   拉取 feed 比较版本 → 确认弹窗 → 下载 Full nupkg 到用户目录缓存 →
   写单条目本地 feed → UpdateManager(本地路径) check → download
   （本地拷贝）→ apply_updates_and_restart。修复国内代理网络下
   velopack（ureq 不读系统代理 + 默认永不超时）"检测不到 / 下载
   永远 0%"的死局。
2. 传输层两端（官方 + 镜像）都失败时，回退 velopack 原生网络流程
   （已加固：镜像源带 HttpOptions 总超时，见 app/core/updater.py；
   检查 / 下载看门狗，停滞即中止；进度框可随时取消；中止后迟到的
   信号一律被 aborted 标记拦下，不会误弹窗、更不会误安装）。

两个入口（均不抛异常，非 Velopack 环境优雅降级）：
- start_startup_check()：主窗口显示后调用，后台静默检查（检查失败
  不出声），发现新版本才弹确认；
- run_manual_check(parent)：设置窗口「检查更新」按钮触发，检查 /
  已是最新 / 不支持环境均有即时提示。

线程模型：检查与下载是阻塞调用，放入 QThreadPool 全局池；控制器
（QObject，主线程）通过跨线程信号（自动队列连接）接收结果。控制器
强引用由模块级 _active 列表持有，防止宿主窗口先关闭时后台任务仍在
发信号；流程终结（已最新 / 失败 / 用户拒绝 / 应用异常）时释放。
确认弹窗、进度框与下载阶段的结果弹窗一律无父窗口，避免宿主窗口
关闭后出现悬垂父引用。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from app.core import updater, updater_net
from app.core.i18n import tr
from app.core.updater_net import UpdateCancelled

logger = logging.getLogger(__name__)

# 回退流程看门狗：检查 60 秒无结果（GithubSource 无超时，可能永久
# 挂起）、下载 90 秒无进展即中止
_LEGACY_CHECK_TIMEOUT_MS = 60_000
_LEGACY_STALL_MS = 90_000

# 进行中流程的控制器强引用（流程终结时移除）
_active_controllers: list["UpdateController"] = []


def _release(controller: "UpdateController") -> None:
    """流程终结：解除强引用（QThreadPool 中的任务自带 controller 引用）。"""
    if controller in _active_controllers:
        _active_controllers.remove(controller)


class UpdateController(QObject):
    """一次更新流程的控制器：后台任务的信号汇聚点（主线程）。"""

    # ---- 自建传输层（首选路径）----
    net_check_finished = Signal(object)     # {"asset","base"} 或 None（已最新）
    net_check_failed = Signal(str)
    net_download_progress = Signal(int)     # 0-100
    net_download_finished = Signal(object)  # {"info","manager"}
    net_download_failed = Signal(str)
    net_download_cancelled = Signal()

    # ---- velopack 原生流程（回退路径）----
    check_finished = Signal(object)         # UpdateInfo（有新版）或 None（已最新）
    check_failed = Signal(str)
    download_progress = Signal(int)         # 0-100
    download_finished = Signal(object)      # UpdateInfo（可应用）
    download_failed = Signal(str)

    def __init__(self, manager) -> None:
        super().__init__()
        self.manager = manager
        # 自建传输层下载的取消标志（进度框取消按钮置位，工作线程轮询）
        self.net_cancel = threading.Event()

    def start_net_check(self) -> None:
        QThreadPool.globalInstance().start(_NetCheckTask(self))

    def start_net_download(self, asset: dict, base: str) -> None:
        QThreadPool.globalInstance().start(_NetDownloadTask(self, asset, base))

    def start_check(self) -> None:
        QThreadPool.globalInstance().start(_CheckTask(self))

    def start_download(self, update_info) -> None:
        QThreadPool.globalInstance().start(_DownloadTask(self, update_info))

    def apply_and_restart(self, update_info) -> None:
        """安装更新并重启（成功时不返回；异常向上抛由调用方兜底）。"""
        self.manager.apply_updates_and_restart(update_info)


class _NetCheckTask(QRunnable):
    """自建传输层检查更新：拉取 feed、与本地已安装版本比较。"""

    def __init__(self, controller: UpdateController) -> None:
        super().__init__()
        self._controller = controller

    def run(self) -> None:
        c = self._controller
        try:
            try:
                current = str(c.manager.get_current_version())
            except Exception as exc:
                c.net_check_failed.emit(f"无法读取当前版本：{exc}")
                return
            feed, base = updater_net.fetch_feed()
            asset = updater_net.pick_full_asset(feed)
            if asset is None or not updater_net.is_newer(asset, current):
                c.net_check_finished.emit(None)
            else:
                c.net_check_finished.emit({"asset": asset, "base": base})
        except Exception as exc:
            logger.warning("自建传输层检查更新失败：%s", exc)
            c.net_check_failed.emit(str(exc))


class _NetDownloadTask(QRunnable):
    """自建传输层下载：Full nupkg 流式下载（官方/镜像互备）→ 写本地
    feed → 本地源 UpdateManager check/download（本地拷贝）。"""

    def __init__(self, controller: UpdateController, asset: dict, base: str) -> None:
        super().__init__()
        self._controller = controller
        self._asset = asset
        self._base = base

    def run(self) -> None:
        c = self._controller
        try:
            cache = updater_net.update_cache_dir()
            # 清空上次残留（旧 nupkg / 中断的 .part），固定目录反复复用
            for stale in cache.iterdir():
                try:
                    stale.unlink()
                except OSError:
                    pass
            bases = [self._base]
            other = (
                updater_net.MIRROR_BASE
                if self._base == updater_net.OFFICIAL_BASE
                else updater_net.OFFICIAL_BASE
            )
            if other not in bases:
                bases.append(other)
            downloaded = False
            errors: list[str] = []
            for base in bases:
                try:
                    updater_net.download_nupkg(
                        base,
                        self._asset,
                        cache,
                        progress=lambda pct: c.net_download_progress.emit(pct),
                        cancelled=c.net_cancel.is_set,
                    )
                    downloaded = True
                    break
                except UpdateCancelled:
                    raise
                except Exception as exc:
                    logger.warning("更新包下载失败（%s）：%s", base, exc)
                    errors.append(f"{base}: {exc}")
            if not downloaded:
                raise updater_net.UpdateNetError("；".join(errors))
            updater_net.build_local_feed(cache, self._asset)
            manager = updater.create_local_update_manager(cache)
            info = manager.check_for_updates()
            if info is None:
                c.net_download_failed.emit("本地更新源未发现新版本")
                return
            manager.download_updates(info)  # 本地拷贝（秒级），无需进度
            c.net_download_finished.emit({"info": info, "manager": manager})
        except UpdateCancelled:
            c.net_download_cancelled.emit()
        except Exception as exc:
            logger.warning("下载 / 准备更新失败：%s", exc)
            c.net_download_failed.emit(str(exc))


class _CheckTask(QRunnable):
    """后台检查更新（回退流程）。"""

    def __init__(self, controller: UpdateController) -> None:
        super().__init__()
        self._controller = controller

    def run(self) -> None:
        try:
            info = self._controller.manager.check_for_updates()
        except Exception as exc:
            logger.warning("检查更新失败：%s", exc)
            self._controller.check_failed.emit(str(exc))
        else:
            self._controller.check_finished.emit(info)


class _DownloadTask(QRunnable):
    """后台下载更新包（回退流程，进度回调跨线程转发为信号）。"""

    def __init__(self, controller: UpdateController, update_info) -> None:
        super().__init__()
        self._controller = controller
        self._info = update_info

    def run(self) -> None:
        try:
            self._controller.manager.download_updates(
                self._info,
                lambda pct: self._controller.download_progress.emit(int(pct)),
            )
        except Exception as exc:
            logger.warning("下载更新失败：%s", exc)
            self._controller.download_failed.emit(str(exc))
        else:
            self._controller.download_finished.emit(self._info)


# ------------------------------------------------------------------ 共享工具


def _version_text(update_info) -> str:
    """UpdateInfo 的目标版本号字符串。"""
    try:
        return str(update_info.TargetFullRelease.Version)
    except Exception:
        return "?"


def _notes_text(update_info) -> str:
    """Release notes（Markdown 原文，可能为空）。"""
    try:
        return (update_info.TargetFullRelease.NotesMarkdown or "").strip()
    except Exception:
        return ""


def _size_text(update_info) -> str:
    """更新包体积提示（如「（约 850 MB）」），未知返回空串。"""
    try:
        size = int(update_info.TargetFullRelease.Size or 0)
    except Exception:
        size = 0
    if size > 0:
        return tr("updater.size_hint", size=f"{size / 1024 / 1024:.0f}")
    return ""


def _ask_confirm(version: str, size_hint: str, notes: str) -> bool:
    """发现新版本的确认弹窗（版本号 + 体积 + release notes）。"""
    body = tr("updater.available_text", version=version, size=size_hint)
    if notes:
        body += "\n\n" + tr("updater.release_notes", notes=notes)
    answer = QMessageBox.question(
        None,
        tr("updater.available_title"),
        body,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


# ------------------------------------------------------------------ 首选流程


def _confirm_and_install_net(
    controller: UpdateController, payload: dict, on_done=None
) -> None:
    """自建传输层：确认弹窗 → requests 流式下载（可取消）→ 本地源安装。

    on_done：流程到达终态（拒绝 / 取消 / 失败 / 应用异常）时回调；
    更新应用成功会直接重启进程，不回调。
    """
    asset = payload["asset"]
    version = str(asset.get("Version") or "?")
    size = int(asset.get("Size") or 0)
    size_hint = (
        tr("updater.size_hint", size=f"{size / 1024 / 1024:.0f}") if size > 0 else ""
    )
    notes = (asset.get("NotesMarkdown") or "").strip()
    if not _ask_confirm(version, size_hint, notes):
        _release(controller)
        if on_done is not None:
            on_done()
        return

    controller.net_cancel.clear()
    progress = QProgressDialog()
    progress.setWindowTitle(tr("updater.download_title"))
    progress.setLabelText(tr("updater.downloading", version=version, pct=0))
    progress.setRange(0, 100)
    progress.setValue(0)
    progress.setCancelable(True)  # requests 流式下载可随时取消
    progress.setMinimumDuration(0)
    progress.show()
    progress.canceled.connect(controller.net_cancel.set)

    def on_progress(pct: int) -> None:
        progress.setLabelText(tr("updater.downloading", version=version, pct=pct))
        progress.setValue(pct)

    def on_finished(bundle: dict) -> None:
        progress.setLabelText(tr("updater.restarting"))
        progress.setValue(100)
        try:
            # 本地目录源：apply 提取 nupkg、替换文件并重启（成功不返回）
            bundle["manager"].apply_updates_and_restart(bundle["info"])
        except Exception as exc:
            logger.error("应用更新失败：%s", exc)
            progress.hide()
            _release(controller)
            if on_done is not None:
                on_done()
            QMessageBox.warning(
                None, tr("updater.download_title"), tr("updater.apply_failed", error=exc)
            )

    def on_failed(error: str) -> None:
        progress.hide()
        _release(controller)
        if on_done is not None:
            on_done()
        QMessageBox.warning(
            None, tr("updater.download_title"), tr("updater.download_failed", error=error)
        )

    def on_cancelled() -> None:
        progress.hide()
        _release(controller)
        if on_done is not None:
            on_done()
        QMessageBox.information(
            None, tr("updater.download_title"), tr("updater.download_cancelled")
        )

    controller.net_download_progress.connect(on_progress)
    controller.net_download_finished.connect(on_finished)
    controller.net_download_failed.connect(on_failed)
    controller.net_download_cancelled.connect(on_cancelled)
    controller.start_net_download(asset, payload["base"])


# ------------------------------------------------------------------ 回退流程


def _start_legacy_check(
    controller: UpdateController, manual: bool, on_done, warn, inform
) -> None:
    """回退：velopack 原生检查（带看门狗——GithubSource 无超时，可能
    永久挂起；挂起线程无法中断，只能放任其阻塞，迟到的信号由
    aborted 标记拦下）。"""

    state = {"aborted": False}
    watchdog = QTimer(controller)
    watchdog.setSingleShot(True)
    watchdog.setInterval(_LEGACY_CHECK_TIMEOUT_MS)

    def finish() -> None:
        watchdog.stop()
        _release(controller)
        if on_done is not None:
            on_done()

    def abort() -> None:
        if state["aborted"]:
            return
        state["aborted"] = True
        finish()
        logger.info(
            "回退检查 %.0f 秒无结果，已中止（后台线程可能仍阻塞）",
            _LEGACY_CHECK_TIMEOUT_MS / 1000,
        )
        warn(tr("updater.check_timeout"))

    def on_finished_check(info) -> None:
        if state["aborted"]:
            return
        if info is not None:
            _confirm_and_install_legacy(controller, info, on_done)
        else:
            finish()
            inform(tr("updater.up_to_date"))

    def on_failed(error: str) -> None:
        if state["aborted"]:
            return
        finish()
        if manual:
            warn(tr("updater.check_failed", error=error))
        else:
            logger.info("启动检查更新失败（静默忽略）：%s", error)

    watchdog.timeout.connect(abort)
    controller.check_finished.connect(on_finished_check)
    controller.check_failed.connect(on_failed)
    watchdog.start()
    controller.start_check()


def _confirm_and_install_legacy(
    controller: UpdateController, update_info, on_done=None
) -> None:
    """回退：确认弹窗 → velopack 下载（看门狗 + 可取消）→ 应用重启。

    velopack 的下载调用无法中断：用户取消 / 看门狗中止只是放弃等待
    （隐藏进度框、释放流程、拦下迟到信号），后台线程可能仍阻塞在
    网络上——但绝不弹窗、更不会因迟到信号误触发安装。
    """
    version = _version_text(update_info)
    if not _ask_confirm(version, _size_text(update_info), _notes_text(update_info)):
        _release(controller)
        if on_done is not None:
            on_done()
        return

    state = {"aborted": False}
    progress = QProgressDialog()
    progress.setWindowTitle(tr("updater.download_title"))
    progress.setLabelText(tr("updater.downloading", version=version, pct=0))
    progress.setRange(0, 100)
    progress.setValue(0)
    progress.setCancelable(True)
    progress.setMinimumDuration(0)
    progress.show()

    stall = QTimer(progress)
    stall.setSingleShot(True)
    stall.setInterval(_LEGACY_STALL_MS)

    def finish() -> None:
        stall.stop()
        progress.hide()
        _release(controller)
        if on_done is not None:
            on_done()

    def abort(stalled: bool) -> None:
        if state["aborted"]:
            return
        state["aborted"] = True
        finish()
        if stalled:
            QMessageBox.warning(
                None, tr("updater.download_title"), tr("updater.download_stalled")
            )
        else:
            QMessageBox.information(
                None, tr("updater.download_title"), tr("updater.download_cancelled")
            )

    def on_progress(pct: int) -> None:
        if state["aborted"]:
            return
        progress.setLabelText(tr("updater.downloading", version=version, pct=pct))
        progress.setValue(pct)
        stall.start()  # 有进展即重置停滞看门狗

    def on_downloaded(info) -> None:
        if state["aborted"]:
            return  # 已中止的下载迟到完成：不安装
        stall.stop()
        progress.setLabelText(tr("updater.restarting"))
        try:
            controller.apply_and_restart(info)  # 成功则进程在此退出
        except Exception as exc:
            logger.error("应用更新失败：%s", exc)
            progress.hide()
            _release(controller)
            if on_done is not None:
                on_done()
            QMessageBox.warning(
                None, tr("updater.download_title"), tr("updater.apply_failed", error=exc)
            )

    def on_download_failed(error: str) -> None:
        if state["aborted"]:
            return
        finish()
        QMessageBox.warning(
            None, tr("updater.download_title"), tr("updater.download_failed", error=error)
        )

    progress.canceled.connect(lambda: abort(False))
    stall.timeout.connect(lambda: abort(True))
    controller.download_progress.connect(on_progress)
    controller.download_finished.connect(on_downloaded)
    controller.download_failed.connect(on_download_failed)
    stall.start()
    controller.start_download(update_info)


# ------------------------------------------------------------------ 两个入口


def _run_flow(manual: bool, parent: QWidget | None, on_done) -> None:
    """统一流程：首选自建传输层，两端失败回退 velopack 原生流程。

    manual：True = 手动检查（全流程有提示）；False = 启动静默检查
    （检查阶段失败仅记日志，下载阶段提示与手动一致）。
    """
    manager = updater.create_update_manager()
    if manager is None:
        if manual:
            QMessageBox.information(parent, tr("updater.check_button"), tr("updater.unsupported"))
        if on_done is not None:
            on_done()
        return
    controller = UpdateController(manager)
    _active_controllers.append(controller)

    def warn(text: str) -> None:
        if manual:
            QMessageBox.warning(parent, tr("updater.check_button"), text)

    def inform(text: str) -> None:
        if manual:
            QMessageBox.information(parent, tr("updater.check_button"), text)

    def on_net_finished(payload) -> None:
        if payload is None:
            _release(controller)
            inform(tr("updater.up_to_date"))
            if on_done is not None:
                on_done()
        else:
            _confirm_and_install_net(controller, payload, on_done)

    def on_net_failed(error: str) -> None:
        logger.warning("自建传输层不可用，回退 velopack 原生流程：%s", error)
        _start_legacy_check(controller, manual, on_done, warn, inform)

    controller.net_check_finished.connect(on_net_finished)
    controller.net_check_failed.connect(on_net_failed)
    controller.start_net_check()


def start_startup_check() -> None:
    """启动后台静默检查：无更新 / 检查失败均不出声（仅记日志）。"""
    _run_flow(manual=False, parent=None, on_done=None)


def run_manual_check(parent: QWidget | None = None, on_done=None) -> None:
    """设置窗口手动检查：全流程有提示（检查中禁用按钮由调用方处理）。

    on_done：流程到达终态（不支持 / 已最新 / 失败 / 拒绝 / 取消 /
    下载或应用失败）时回调；更新应用成功直接重启进程，不回调。
    """
    _run_flow(manual=True, parent=parent, on_done=on_done)
