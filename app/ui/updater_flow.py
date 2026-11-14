"""自动更新的 UI 编排：后台线程检查 / 下载，信号回主线程。

流程（2026-11 破局改造，两层）：
1. 首选自建传输层（app/core/updater_net.py：requests 走系统代理、
   连接/读超时真实生效、流式下载可随时中止——与浏览器同栈）：
   拉取 feed 比较版本 → 确认弹窗 → 下载更新包到用户目录缓存
   （增量优先：有 Delta 条目时只下载几 MB 的补丁包，由 velopack
   用本机 packages 目录的基线 full 包重建目标 full 包，任一环节
   失败自动改走全量下载）→ 写本地 feed → UpdateManager(本地路径)
   check → download（本地拷贝）→ 询问重启时机 → 立即重启或稍后
   安装（下次启动生效）。修复国内代理网络下
   velopack（ureq 不读系统代理 + 默认永不超时）"检测不到 / 下载
   永远 0%"的死局。
2. 传输层两端（官方 + 镜像）都失败时，回退 velopack 原生网络流程
   （已加固：镜像源带 HttpOptions 总超时，见 app/core/updater.py；
   检查 / 下载看门狗，停滞即中止；中止后迟到的信号一律被 aborted
   标记拦下，不会误弹窗、更不会误安装）。

下载进度窗（_UpdateProgressDialog）：
- 无 Cancel 按钮（0.1.4 的教训：requests 读超时 + 看门狗保证失败
  必达终态，无需手动逃生口）；标题栏关闭（X）等同取消（rejected
  信号 → 置取消标志 + 结束流程）；
- 内嵌两条手动下载链接（GitHub Releases / gh-proxy 镜像，点击用
  系统默认浏览器打开）——下载失败或过慢时的逃生通道；失败警告框
  同样附带链接（发现新版本的确认弹窗不带链接，保持简洁）。

日志与异常防护（0.1.4 卡 0% 的直接教训：Qt 槽内异常只进不可见的
stderr，冻结版无控制台彻底无声）：
- 全部槽函数与流程函数包 try/except，异常带堆栈写入启动日志
  （%LOCALAPPDATA%\\ESubDub\\startup日期.log）并弹错误框。

两个入口（均不抛异常，非 Velopack 环境优雅降级）：
- start_startup_check()：主窗口显示后调用，后台静默检查（检查失败
  不出声），发现新版本才弹确认；
- run_manual_check(parent)：设置窗口「检查更新」按钮触发，检查 /
  已是最新 / 不支持环境均有即时提示。

线程模型：检查与下载是阻塞调用，放入 QThreadPool 全局池；控制器
（QObject，主线程）通过跨线程信号（自动队列连接）接收结果。控制器
强引用由模块级 _active 列表持有，防止宿主窗口先关闭时后台任务仍在
发信号；流程终结（已最新 / 失败 / 用户拒绝 / 关闭窗口 / 应用异常）
时释放。确认弹窗、进度框与下载阶段的结果弹窗一律无父窗口，避免
宿主窗口关闭后出现悬垂父引用。
"""

from __future__ import annotations

import logging
import threading

from PySide6.QtCore import (
    QObject,
    QRunnable,
    QThreadPool,
    QTimer,
    Signal,
    Qt,
)
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QVBoxLayout,
    QWidget,
)

from app.core import updater, updater_net
from app.core.i18n import tr
from app.core.updater_net import UpdateCancelled

logger = logging.getLogger(__name__)

# 回退流程看门狗：检查 60 秒无结果（GithubSource 无超时，可能永久
# 挂起）即中止；下载 90 秒无进展即中止
_LEGACY_CHECK_TIMEOUT_MS = 60_000
_LEGACY_STALL_MS = 90_000
# 自建传输层看门狗：requests 自带 30 秒读超时（数据流停滞即报错），
# 60 秒无进度变化才触发——双保险，主要拦截"信号链断裂"类问题，
# 不至于误伤慢速网络（1% 进度间隔 < 60 秒）
_NET_STALL_MS = 60_000

# 手动下载逃生链接（与下载窗口 / 确认弹窗 / 失败警告框共用）
_GITHUB_RELEASES_URL = "https://github.com/yangjinduo/E-SubDub/releases"
_MIRROR_URL = (
    "https://gh-proxy.com/#https://github.com/yangjinduo/E-SubDub/releases"
)

# 进行中流程的控制器强引用（流程终结时移除）
_active_controllers: list["UpdateController"] = []


def _release(controller: "UpdateController") -> None:
    """流程终结：解除强引用（QThreadPool 中的任务自带 controller 引用）。"""
    if controller in _active_controllers:
        _active_controllers.remove(controller)


def _present_on_top(dialog: QDialog) -> None:
    """把进度窗顶到最前。

    进度窗无父窗口（防悬垂引用）且 WindowModal 对无父窗口等于非模
    态——主窗口或其他窗口一旦抢占焦点就会把它压到底层（实测"闪一
    下就到最下层"）。show 之后立即 raise + activate，并延时再补一次，
    对抗 Windows 的焦点竞争。
    """

    def _raise() -> None:
        if dialog.isVisible():
            dialog.raise_()
            dialog.activateWindow()

    _raise()
    QTimer.singleShot(100, _raise)


# ------------------------------------------------------------------ 共享工具


def _manual_links_html() -> str:
    """手动下载链接的富文本（QLabel / QMessageBox 富文本自动外链打开）。"""
    return (
        f'{tr("updater.manual_links_hint")}<br>'
        f'<a href="{_GITHUB_RELEASES_URL}">{tr("updater.link_github")}</a><br>'
        f'<a href="{_MIRROR_URL}">{tr("updater.link_mirror")}</a>'
    )


def _warn_with_links(title: str, message: str) -> None:
    """带手动下载链接的警告框（下载失败 / 看门狗中止时提示逃生通道）。"""
    QMessageBox.warning(None, title, f"{message}<br><br>{_manual_links_html()}")


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
    """发现新版本的确认弹窗（版本号 + 体积 + release notes）。

    显式指定富文本格式：QMessageBox 的自动探测按"开头是否像标签"
    判断，正文以普通文字开头会被当成纯文本，notes 里的 HTML 连标签
    一起原样显示（0.1.4 实测的"满屏 HTML"）。手动下载链接只出现在
    下载窗口与失败警告框，这里保持简洁。
    """
    body = tr("updater.available_text", version=version, size=size_hint)
    if notes:
        body += "<br><br>" + tr(
            "updater.release_notes", notes=notes.replace("\n", "<br>")
        )
    box = QMessageBox()
    box.setWindowTitle(tr("updater.available_title"))
    box.setIcon(QMessageBox.Icon.Question)
    box.setTextFormat(Qt.TextFormat.RichText)
    box.setText(body)
    box.setStandardButtons(
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    box.setDefaultButton(QMessageBox.StandardButton.No)
    return box.exec() == QMessageBox.StandardButton.Yes


def _ask_restart() -> bool:
    """更新包下载完成后的询问：立即重启（True）还是稍后重启（False）。"""
    box = QMessageBox()
    box.setWindowTitle(tr("updater.restart_title"))
    box.setIcon(QMessageBox.Icon.Question)
    box.setText(tr("updater.restart_text"))
    now = box.addButton(tr("updater.restart_now"), QMessageBox.ButtonRole.YesRole)
    later = box.addButton(tr("updater.restart_later"), QMessageBox.ButtonRole.NoRole)
    box.setDefaultButton(now)
    box.exec()
    return box.clickedButton() is now


def _install_update(update_info, manager, finish) -> None:
    """下载完成后安装：先询问重启时机再动手。

    立即重启 → apply_updates_and_restart（成功则进程在此退出，不返
    回），失败向上抛由调用方统一记日志 + 弹错误框。
    稍后重启 → wait_exit_then_apply_updates 注册"本进程退出后自动安
    装、不重新拉起"（注意：velopack 绑定没有不带重启的 apply_updates
    方法，0.1.5 实测调用即 AttributeError）。这步即使失败也不算错误：
    download_updates 已把更新包落进 velopack 本地包目录，主程序入口
    的 VelopackApp().run() 会在下次启动时自动应用（auto apply on
    startup），所以只记日志不弹窗。注册成功后静默收尾，不再弹提示
    ——用户点"稍后重启"已表明明白更新何时生效。
    """
    if _ask_restart():
        manager.apply_updates_and_restart(update_info)
        return
    try:
        manager.wait_exit_then_apply_updates(
            update_info, silent=True, restart=False
        )
    except Exception:
        logger.exception("注册退出后自动安装失败（下次启动仍会自动应用）")
    finish()


# ------------------------------------------------------------------ 进度窗口


class _UpdateProgressDialog(QDialog):
    """下载更新进度窗：状态行 + 进度条 + 手动下载链接。

    无 Cancel 按钮：requests 读超时 + 看门狗保证失败必达终态；标题栏
    关闭（X）等同取消（rejected 信号由调用方接取消逻辑）。手动下载
    链接是失败 / 过慢时的逃生通道，点击用系统默认浏览器打开。
    """

    def __init__(self, title: str) -> None:
        super().__init__(None)
        self.setWindowTitle(title)
        self.setWindowModality(Qt.WindowModality.WindowModal)
        self._status = QLabel()
        self._status.setWordWrap(True)
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._links = QLabel(_manual_links_html())
        self._links.setOpenExternalLinks(True)
        self._links.setWordWrap(True)
        layout = QVBoxLayout(self)
        layout.addWidget(self._status)
        layout.addWidget(self._bar)
        layout.addWidget(self._links)

    def set_status(self, text: str) -> None:
        self._status.setText(text)

    def set_progress(self, pct: int) -> None:
        self._bar.setValue(pct)


# ------------------------------------------------------------------ 控制器


class UpdateController(QObject):
    """一次更新流程的控制器：后台任务的信号汇聚点（主线程）。"""

    # ---- 自建传输层（首选路径）----
    net_check_finished = Signal(object)     # {"full","delta","base"} 或 None（已最新）
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
        # 自建传输层下载的取消标志（进度窗关闭(X)/看门狗中止时置位，
        # 工作线程轮询）
        self.net_cancel = threading.Event()

    def start_net_check(self) -> None:
        QThreadPool.globalInstance().start(_NetCheckTask(self))

    def start_net_download(self, full: dict, delta: dict | None, base: str) -> None:
        QThreadPool.globalInstance().start(_NetDownloadTask(self, full, delta, base))

    def start_check(self) -> None:
        QThreadPool.globalInstance().start(_CheckTask(self))

    def start_download(self, update_info) -> None:
        QThreadPool.globalInstance().start(_DownloadTask(self, update_info))


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
                logger.exception("读取当前版本失败")
                c.net_check_failed.emit(f"无法读取当前版本：{exc}")
                return
            feed, base = updater_net.fetch_feed()
            full = updater_net.pick_full_asset(feed)
            if full is None or not updater_net.is_newer(full, current):
                c.net_check_finished.emit(None)
            else:
                delta = updater_net.pick_delta_asset(feed, full)
                if delta is not None:
                    logger.info(
                        "发现增量更新：目标 %s（delta %.1f MB / full %.1f MB）",
                        full.get("Version"),
                        (delta.get("Size") or 0) / 1024 / 1024,
                        (full.get("Size") or 0) / 1024 / 1024,
                    )
                c.net_check_finished.emit(
                    {"full": full, "delta": delta, "base": base}
                )
        except Exception as exc:
            logger.exception("自建传输层检查更新失败")
            c.net_check_failed.emit(str(exc))


class _NetDownloadTask(QRunnable):
    """自建传输层下载：增量优先，失败自动改走全量。

    增量路径：requests 下载 delta nupkg（几 MB）→ 本地 feed 写
    Full+Delta 两条目 → 本地源 UpdateManager check/download——
    velopack 自行用本机 packages 目录的基线 full 包执行 Update.exe
    patch 重建目标 full 包；基线不匹配 / 补丁损坏时 zstd 字典解码
    校验失败，velopack 内部先试从本地源取 Full 回退（全量文件未
    下载、必然失败），最终抛错到这里——统一改走全量路径。

    全量路径：requests 下载 full nupkg（百余 MB）→ 单条目 feed →
    本地 check/download（秒级本地拷贝）。
    """

    def __init__(
        self,
        controller: UpdateController,
        full: dict,
        delta: dict | None,
        base: str,
    ) -> None:
        super().__init__()
        self._controller = controller
        self._full = full
        self._delta = delta
        self._base = base

    # 清空下载缓存（旧 nupkg / 中断的 .part / 上一路径的半成品），
    # 固定目录反复复用，不产生孤儿临时文件
    @staticmethod
    def _clean_cache(cache) -> None:
        for stale in cache.iterdir():
            try:
                stale.unlink()
            except OSError:
                pass

    def _bases(self) -> list[str]:
        bases = [self._base]
        other = (
            updater_net.MIRROR_BASE
            if self._base == updater_net.OFFICIAL_BASE
            else updater_net.OFFICIAL_BASE
        )
        if other not in bases:
            bases.append(other)
        return bases

    def _download(self, asset: dict, cache) -> None:
        """官方/镜像互备下载一个 nupkg 条目（进度/取消接控制器）。"""
        c = self._controller
        errors: list[str] = []
        for base in self._bases():
            try:
                updater_net.download_nupkg(
                    base,
                    asset,
                    cache,
                    progress=lambda pct: c.net_download_progress.emit(pct),
                    cancelled=c.net_cancel.is_set,
                )
                return
            except UpdateCancelled:
                raise
            except Exception as exc:
                logger.warning("更新包下载失败（%s）：%s", base, exc)
                errors.append(f"{base}: {exc}")
        raise updater_net.UpdateNetError("；".join(errors))

    def _check_and_download(self, cache, feed_assets: list[dict]):
        """写本地 feed → 本地源 check → download（返回 UpdateInfo）。

        download 的进度回调照常转发：本地源拷贝有进度；增量路径的
        patch 重建阶段（zstd 解压基线 + 打补丁 + 重压缩，约十几秒）
        不产进度信号——60 秒下载看门狗期间恰好覆盖，真机验证后如
        有误杀再调看门狗。
        """
        c = self._controller
        updater_net.build_local_feed(cache, feed_assets)
        manager = updater.create_local_update_manager(cache)
        info = manager.check_for_updates()
        if info is None:
            raise updater_net.UpdateNetError("本地更新源未发现新版本")
        manager.download_updates(
            info, lambda pct: c.net_download_progress.emit(int(pct))
        )
        return info, manager

    def run(self) -> None:
        c = self._controller
        try:
            cache = updater_net.update_cache_dir()
            # 增量路径：任一环节失败 → 清缓存改走全量（用户取消除外）
            if self._delta is not None:
                try:
                    self._clean_cache(cache)
                    self._download(self._delta, cache)
                    info, manager = self._check_and_download(
                        cache, [self._full, self._delta]
                    )
                    c.net_download_finished.emit({"info": info, "manager": manager})
                    return
                except UpdateCancelled:
                    raise
                except Exception as exc:
                    logger.warning("增量更新失败，改走全量下载：%s", exc)
            # 全量路径
            self._clean_cache(cache)
            self._download(self._full, cache)
            info, manager = self._check_and_download(cache, [self._full])
            c.net_download_finished.emit({"info": info, "manager": manager})
        except UpdateCancelled:
            c.net_download_cancelled.emit()
        except Exception as exc:
            logger.exception("下载 / 准备更新失败")
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
            logger.exception("回退流程检查更新失败")
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
            logger.exception("回退流程下载更新失败")
            self._controller.download_failed.emit(str(exc))
        else:
            self._controller.download_finished.emit(self._info)


# ------------------------------------------------------------------ 首选流程


def _confirm_and_install_net(
    controller: UpdateController, payload: dict, on_done=None
) -> None:
    """自建传输层：确认弹窗 → requests 流式下载 → 本地源安装。

    on_done：流程到达终态（拒绝 / 关闭窗口 / 取消 / 失败 / 应用异常 /
    选择稍后重启）时回调；选择立即重启且成功则直接重启进程，不回调。
    """
    full = payload["full"]
    delta = payload.get("delta")
    version = str(full.get("Version") or "?")
    # 体积提示按实际下载量：有增量时显示补丁体积（几 MB），否则全量
    size_asset = delta if delta is not None else full
    size = int(size_asset.get("Size") or 0)
    size_hint = (
        tr("updater.size_hint", size=f"{size / 1024 / 1024:.0f}") if size > 0 else ""
    )
    notes = (full.get("NotesMarkdown") or "").strip()
    if not _ask_confirm(version, size_hint, notes):
        _release(controller)
        if on_done is not None:
            on_done()
        return

    # state["done"] 保证终态处理只执行一次（X 关闭、取消、失败、完成
    # 可能交错到达）
    state = {"done": False}
    progress: _UpdateProgressDialog | None = None
    try:
        controller.net_cancel.clear()
        progress = _UpdateProgressDialog(tr("updater.download_title"))
        progress.set_status(tr("updater.downloading", version=version, pct=0))
        progress.show()
        _present_on_top(progress)

        stall = QTimer(progress)
        stall.setSingleShot(True)
        stall.setInterval(_NET_STALL_MS)

        def finish() -> None:
            """终态只处理一次：停看门狗、关窗、释放流程、回调。"""
            if state["done"]:
                return
            state["done"] = True
            stall.stop()
            progress.hide()
            _release(controller)
            if on_done is not None:
                on_done()

        def on_stall() -> None:
            """看门狗：60 秒无进度变化（requests 30 秒读超时的双保险，
            主要拦截信号链断裂类问题——0.1.4 卡 0% 的教训）。"""
            if state["done"]:
                return
            logger.warning("下载 %.0f 秒无进展，看门狗中止", _NET_STALL_MS / 1000)
            finish()
            _warn_with_links(
                tr("updater.download_title"), tr("updater.download_stalled")
            )

        def on_progress(pct: int) -> None:
            if state["done"]:
                return
            progress.set_status(tr("updater.downloading", version=version, pct=pct))
            progress.set_progress(pct)
            stall.start()  # 有进展即重置看门狗

        def on_finished(bundle: dict) -> None:
            if state["done"]:
                return
            stall.stop()
            progress.set_progress(100)
            try:
                # 询问立即 / 稍后重启；立即且成功则进程在此退出不返回
                _install_update(bundle["info"], bundle["manager"], finish)
            except Exception as exc:
                logger.exception("应用更新失败")
                finish()
                QMessageBox.warning(
                    None,
                    tr("updater.download_title"),
                    tr("updater.apply_failed", error=exc),
                )

        def on_failed(error: str) -> None:
            if state["done"]:
                return
            finish()
            _warn_with_links(
                tr("updater.download_title"), tr("updater.download_failed", error=error)
            )

        def on_cancelled() -> None:
            if state["done"]:
                return
            finish()  # 用户主动关闭，静默收尾

        def on_rejected() -> None:
            """标题栏关闭（X）= 取消：置取消标志（后台线程轮询退出）。"""
            controller.net_cancel.set()
            finish()

        progress.rejected.connect(on_rejected)
        controller.net_download_progress.connect(on_progress)
        controller.net_download_finished.connect(on_finished)
        controller.net_download_failed.connect(on_failed)
        controller.net_download_cancelled.connect(on_cancelled)
        stall.start()
        controller.start_net_download(full, delta, payload["base"])
    except Exception as exc:
        # 0.1.4 的教训：冻结版无控制台，槽内异常无声消失，进度窗变
        # 僵尸——这里兜底：日志 + 关窗 + 错误框 + 逃生链接
        logger.exception("下载流程发生未预期异常")
        if progress is not None:
            progress.hide()
        _release(controller)
        if on_done is not None:
            on_done()
        _warn_with_links(
            tr("updater.download_title"),
            tr("updater.download_failed", error=f"内部错误（详见启动日志）：{exc}"),
        )


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
            try:
                _confirm_and_install_legacy(controller, info, on_done)
            except Exception as exc:
                logger.exception("回退确认/下载流程异常")
                _release(controller)
                if on_done is not None:
                    on_done()
                if manual:
                    QMessageBox.warning(
                        None,
                        tr("updater.check_button"),
                        tr("updater.check_failed", error=f"内部错误：{exc}"),
                    )
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
    """回退：确认弹窗 → velopack 下载（看门狗 + X 关闭即中止）→ 应用重启。

    velopack 的下载调用无法中断：用户关闭窗口 / 看门狗中止只是放弃
    等待（隐藏进度框、释放流程、拦下迟到信号），后台线程可能仍阻塞
    在网络上——但绝不弹窗、更不会因迟到信号误触发安装。
    """
    version = _version_text(update_info)
    if not _ask_confirm(version, _size_text(update_info), _notes_text(update_info)):
        _release(controller)
        if on_done is not None:
            on_done()
        return

    state = {"aborted": False}
    progress: _UpdateProgressDialog | None = None
    try:
        progress = _UpdateProgressDialog(tr("updater.download_title"))
        progress.set_status(tr("updater.downloading", version=version, pct=0))
        progress.show()
        _present_on_top(progress)

        stall = QTimer(progress)
        stall.setSingleShot(True)
        stall.setInterval(_LEGACY_STALL_MS)

        def finish() -> None:
            if state["aborted"]:
                return
            state["aborted"] = True
            stall.stop()
            progress.hide()
            _release(controller)
            if on_done is not None:
                on_done()

        def abort(stalled: bool) -> None:
            """stalled=True：看门狗触发（提示逃生链接）；False：用户关闭窗口
            （静默收尾——velopack 线程无法中断，只能放弃等待）。"""
            if state["aborted"]:
                return
            state["aborted"] = True
            stall.stop()
            progress.hide()
            _release(controller)
            if on_done is not None:
                on_done()
            if stalled:
                logger.warning(
                    "回退下载 %.0f 秒无进展，看门狗中止",
                    _LEGACY_STALL_MS / 1000,
                )
                _warn_with_links(
                    tr("updater.download_title"), tr("updater.download_stalled")
                )

        def on_progress(pct: int) -> None:
            if state["aborted"]:
                return
            progress.set_status(tr("updater.downloading", version=version, pct=pct))
            progress.set_progress(pct)
            stall.start()  # 有进展即重置停滞看门狗

        def on_downloaded(info) -> None:
            if state["aborted"]:
                return  # 已中止的下载迟到完成：不安装
            stall.stop()
            progress.set_progress(100)
            try:
                # 询问立即 / 稍后重启；立即且成功则进程在此退出不返回
                _install_update(info, controller.manager, finish)
            except Exception as exc:
                logger.exception("回退流程应用更新失败")
                finish()
                QMessageBox.warning(
                    None,
                    tr("updater.download_title"),
                    tr("updater.apply_failed", error=exc),
                )

        def on_download_failed(error: str) -> None:
            if state["aborted"]:
                return
            stall.stop()
            state["aborted"] = True
            progress.hide()
            _release(controller)
            if on_done is not None:
                on_done()
            _warn_with_links(
                tr("updater.download_title"), tr("updater.download_failed", error=error)
            )

        progress.rejected.connect(lambda: abort(False))
        controller.download_progress.connect(on_progress)
        controller.download_finished.connect(on_downloaded)
        controller.download_failed.connect(on_download_failed)
        stall.start()
        controller.start_download(update_info)
    except Exception as exc:
        logger.exception("回退下载流程发生未预期异常")
        if progress is not None:
            progress.hide()
        _release(controller)
        if on_done is not None:
            on_done()
        _warn_with_links(
            tr("updater.download_title"),
            tr("updater.download_failed", error=f"内部错误（详见启动日志）：{exc}"),
        )


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
        try:
            if payload is None:
                _release(controller)
                inform(tr("updater.up_to_date"))
                if on_done is not None:
                    on_done()
            else:
                _confirm_and_install_net(controller, payload, on_done)
        except Exception as exc:
            # 0.1.4 的教训：槽内异常进不可见的 stderr——兜底到日志+弹窗
            logger.exception("处理检查结果时发生异常")
            _release(controller)
            if on_done is not None:
                on_done()
            QMessageBox.critical(
                None,
                tr("updater.check_button"),
                tr("updater.check_failed", error=f"内部错误（详见启动日志）：{exc}"),
            )

    def on_net_failed(error: str) -> None:
        try:
            logger.warning("自建传输层不可用，回退 velopack 原生流程：%s", error)
            _start_legacy_check(controller, manual, on_done, warn, inform)
        except Exception as exc:
            logger.exception("回退流程启动异常")
            _release(controller)
            if on_done is not None:
                on_done()
            QMessageBox.critical(
                None,
                tr("updater.check_button"),
                tr("updater.check_failed", error=f"内部错误（详见启动日志）：{exc}"),
            )

    controller.net_check_finished.connect(on_net_finished)
    controller.net_check_failed.connect(on_net_failed)
    controller.start_net_check()


def start_startup_check() -> None:
    """启动后台静默检查：无更新 / 检查失败均不出声（仅记日志）。"""
    _run_flow(manual=False, parent=None, on_done=None)


def run_manual_check(parent: QWidget | None = None, on_done=None) -> None:
    """设置窗口手动检查：全流程有提示（检查中禁用按钮由调用方处理）。

    on_done：流程到达终态（不支持 / 已最新 / 失败 / 拒绝 / 关闭窗口 /
    下载或应用失败 / 选择稍后重启）时回调；选择立即重启且成功则直接
    重启进程，不回调。
    """
    _run_flow(manual=True, parent=parent, on_done=on_done)
