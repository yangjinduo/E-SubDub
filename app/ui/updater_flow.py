"""自动更新的 UI 编排：后台线程检查 / 下载，信号回主线程。

两个入口（均不抛异常，非 Velopack 环境优雅降级）：
- start_startup_check()：主窗口显示后调用，后台静默检查（不阻塞
  UI、无加载态），发现新版本才弹确认；
- run_manual_check(parent)：设置窗口「检查更新」按钮触发，检查 /
  已是最新 / 不支持环境均有即时提示。

线程模型：检查与下载是阻塞网络调用，放入 QThreadPool 全局池；
控制器（QObject，主线程）通过跨线程信号（自动队列连接）接收结果。
控制器强引用由模块级 _active 列表持有，防止宿主窗口先关闭时后台
任务仍在发信号；流程终结（已最新 / 失败 / 用户拒绝 / 应用异常）时
释放。确认弹窗、进度框与结果弹窗一律无父窗口，避免宿主窗口关闭
后出现悬垂父引用。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import QMessageBox, QProgressDialog, QWidget

from app.core import updater
from app.core.i18n import tr

logger = logging.getLogger(__name__)

# 进行中流程的控制器强引用（流程终结时移除）
_active_controllers: list["UpdateController"] = []


def _release(controller: "UpdateController") -> None:
    """流程终结：解除强引用（QThreadPool 中的任务自带 manager 引用）。"""
    if controller in _active_controllers:
        _active_controllers.remove(controller)


class UpdateController(QObject):
    """一次更新流程的控制器：后台任务的信号汇聚点（主线程）。"""

    check_finished = Signal(object)  # UpdateInfo（有新版）或 None（已最新）
    check_failed = Signal(str)
    download_progress = Signal(int)  # 0-100
    download_finished = Signal(object)  # UpdateInfo（可应用）
    download_failed = Signal(str)

    def __init__(self, manager) -> None:
        super().__init__()
        self.manager = manager

    def start_check(self) -> None:
        QThreadPool.globalInstance().start(_CheckTask(self))

    def start_download(self, update_info) -> None:
        QThreadPool.globalInstance().start(_DownloadTask(self, update_info))

    def apply_and_restart(self, update_info) -> None:
        """安装更新并重启（成功时不返回；异常向上抛由调用方兜底）。"""
        self.manager.apply_updates_and_restart(update_info)


class _CheckTask(QRunnable):
    """后台检查更新。"""

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
    """后台下载更新包（进度回调跨线程转发为信号）。"""

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


# ------------------------------------------------------------------ 共享流程


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


def _confirm_and_install(
    controller: UpdateController, update_info, on_done=None
) -> None:
    """确认弹窗（版本号 + release notes）→ 下载（进度框）→ 应用并重启。

    on_done：流程到达终态（拒绝 / 失败 / 应用异常）时回调（更新应用
    成功会直接重启进程，不回调）；宿主窗口可能已销毁，回调内部需自理。
    """
    version = _version_text(update_info)
    body = tr("updater.available_text", version=version, size=_size_text(update_info))
    notes = _notes_text(update_info)
    if notes:
        body += "\n\n" + tr("updater.release_notes", notes=notes)
    answer = QMessageBox.question(
        None,
        tr("updater.available_title"),
        body,
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    if answer != QMessageBox.StandardButton.Yes:
        _release(controller)
        if on_done is not None:
            on_done()
        return

    progress = QProgressDialog()
    progress.setWindowTitle(tr("updater.download_title"))
    progress.setLabelText(tr("updater.downloading", version=version, pct=0))
    progress.setRange(0, 100)
    progress.setValue(0)
    progress.setCancelable(False)
    progress.setMinimumDuration(0)
    progress.show()

    def on_progress(pct: int) -> None:
        progress.setLabelText(tr("updater.downloading", version=version, pct=pct))
        progress.setValue(pct)

    def on_downloaded(info) -> None:
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
        progress.hide()
        _release(controller)
        if on_done is not None:
            on_done()
        QMessageBox.warning(
            None, tr("updater.download_title"), tr("updater.download_failed", error=error)
        )

    controller.download_progress.connect(on_progress)
    controller.download_finished.connect(on_downloaded)
    controller.download_failed.connect(on_download_failed)
    controller.start_download(update_info)


# ------------------------------------------------------------------ 两个入口


def start_startup_check() -> None:
    """启动后台静默检查：无更新 / 失败均不出声（仅记日志）。"""
    manager = updater.create_update_manager()
    if manager is None:
        return  # 非 Velopack 安装环境：静默跳过
    controller = UpdateController(manager)
    _active_controllers.append(controller)

    def on_finished(info) -> None:
        if info is not None:
            _confirm_and_install(controller, info)
        else:
            _release(controller)

    def on_failed(error: str) -> None:
        logger.info("启动检查更新失败（静默忽略）：%s", error)
        _release(controller)

    controller.check_finished.connect(on_finished)
    controller.check_failed.connect(on_failed)
    controller.start_check()


def run_manual_check(parent: QWidget | None = None, on_done=None) -> None:
    """设置窗口手动检查：全流程有提示（检查中禁用按钮由调用方处理）。

    on_done：流程到达终态（不支持 / 已最新 / 失败 / 拒绝 / 下载或
    应用失败）时回调；更新应用成功直接重启进程，不回调。
    """
    manager = updater.create_update_manager()
    if manager is None:
        QMessageBox.information(parent, tr("updater.check_button"), tr("updater.unsupported"))
        if on_done is not None:
            on_done()
        return
    controller = UpdateController(manager)
    _active_controllers.append(controller)

    def on_finished(info) -> None:
        if info is not None:
            _confirm_and_install(controller, info, on_done=on_done)
        else:
            _release(controller)
            QMessageBox.information(parent, tr("updater.check_button"), tr("updater.up_to_date"))
            if on_done is not None:
                on_done()

    def on_failed(error: str) -> None:
        _release(controller)
        QMessageBox.warning(parent, tr("updater.check_button"), tr("updater.check_failed", error=error))
        if on_done is not None:
            on_done()

    controller.check_finished.connect(on_finished)
    controller.check_failed.connect(on_failed)
    controller.start_check()
