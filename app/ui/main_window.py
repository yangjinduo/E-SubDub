"""主窗口：顶部资源状态栏 + 中部任务列表 + 底部系统监视栏。

任务配置与执行监控移入独立的任务窗口（点任务列表"＋"创建）；
本窗口只承担队列总览：任务状态/进度一览、双击打开任务窗口。
语言切换时通过 _build_ui() 整体重建（TaskManager 挂在主窗口本体，
队列与任务窗口不受重建影响）。

启动期的精确设备检测（需导入 torch，数秒）与运行环境状态检查在
_StartupProbe 守护线程中执行：窗口先用快速粗检测结果立即显示，
探测完成后经信号回主线程刷新设备卡与徽标，期间窗口始终可操作。
"""

import threading

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMainWindow, QMessageBox, QWidget, QVBoxLayout

from app.core.config import AppConfig
from app.core.hardware import DeviceInfo, detect_device, detect_device_standalone
from app.core.i18n import tr
from app.core.task_manager import TaskManager
from app.resources.env_setup import environment_status, get_env_setup_manager
from app.resources.model import get_resource_manager
from app.ui.dialogs.resource_dialog import SettingsDialog
from app.ui.dialogs.task_dialog import TaskWindow
from app.ui.widgets.resource_bar import ResourceStatusBar
from app.ui.widgets.system_monitor import SystemMonitorBar
from app.ui.widgets.task_list import TaskListPanel


class _StartupProbe(QObject):
    """启动期后台探测：torch 精确设备检测 + 运行环境状态检查。

    detect_device() 首次调用需导入 torch（数秒）、environment_status()
    做 .venv-tts / llama-server 文件级检查；两者放守护线程执行，结果经
    result_ready 信号回主线程，探测期间不阻塞 UI。守护线程不持有
    QThread 对象，应用退出时无需等待。
    """

    result_ready = Signal(object, object)  # DeviceInfo, environment_status() 字典

    def start(self) -> None:
        threading.Thread(
            target=self._run, daemon=True, name="startup-probe",
        ).start()

    def _run(self) -> None:
        device = detect_device()
        env = environment_status()
        try:
            self.result_ready.emit(device, env)
        except RuntimeError:
            pass  # 主窗口已销毁（应用启动后立即退出的极端情况）


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(
        self,
        device_info: DeviceInfo | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 资源管理器单例
        self._manager = get_resource_manager()
        # 当前显示的设备信息：启动时为不依赖 torch 的快速粗检测结果，
        # _StartupProbe 完成精确检测后经信号替换
        self._device: DeviceInfo = device_info or detect_device_standalone()
        # 运行环境（TTS 虚拟环境 / LLAMA 服务）就绪状态，设备卡徽标用；
        # 探测完成前按上次缓存（首启为未就绪）显示
        self._env_state: tuple[bool, bool] = (False, False)
        # 任务队列（挂在主窗口本体上：语言切换重建 central widget 不受影响）
        self._task_manager = TaskManager(parent=self)
        self._task_manager.device = self._device.device
        # 环境搭建完成后刷新设备卡徽标（设置窗口触发，窗口关闭后仍生效）
        get_env_setup_manager().environment_changed.connect(self._on_env_changed)
        # 打开中的任务窗口（含未开始的空窗口；应用退出时统一销毁）
        self._task_windows: list[TaskWindow] = []
        # 启动期后台探测（精确设备 + 运行环境状态），完成后信号刷新
        self._probe = _StartupProbe()
        self._probe.result_ready.connect(self._on_probe_ready)
        self._build_ui()
        self._probe.start()

    # ------------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        """构建全部界面；语言切换时可重复调用（旧的 central widget 被自动销毁）。"""
        # 语言切换重建前停掉旧的监视采样线程
        if hasattr(self, "_monitor"):
            self._monitor.stop()

        self.setWindowTitle(f"{tr('main.title')} v{AppConfig.APP_VERSION}")
        self.setMinimumSize(1100, 750)  # 最小尺寸，窗口可自由缩放

        # ------------------------------ 顶部：资源状态栏 ------------------------------
        self._resource_bar = ResourceStatusBar(self._manager)
        self._resource_bar.set_device(self._device)
        # 环境徽标先按缓存状态显示，探测完成后由 _on_probe_ready 刷新
        self._resource_bar.set_env_state(*self._env_state)
        self._resource_bar.settings_requested.connect(self._open_settings_dialog)

        # ------------------------------ 中部：任务列表 ------------------------------
        self._task_list = TaskListPanel(self._task_manager)
        self._task_list.add_requested.connect(self._open_new_task_window)
        self._task_list.task_activated.connect(self._activate_task_window)

        # ------------------------------ 底部：系统监视栏 ------------------------------
        self._monitor = SystemMonitorBar()

        # ------------------------------ 总装 ------------------------------
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._resource_bar)
        layout.addWidget(self._task_list, 1)
        layout.addWidget(self._monitor)
        self.setCentralWidget(central)

    # ------------------------------------------------------------------ 设备 / 环境

    def _on_probe_ready(self, device: DeviceInfo, env: dict) -> None:
        """启动探测完成：刷新设备卡、环境徽标与队列推理设备。"""
        self._device = device
        self._env_state = (env["tts_ready"], env["llama_ready"])
        self._resource_bar.set_device(self._device)
        self._resource_bar.set_env_state(*self._env_state)
        self._task_manager.device = self._device.device

    # ------------------------------------------------------------------ 运行环境

    def _refresh_env_state(self) -> None:
        """检测运行环境（TTS 虚拟环境 / LLAMA 服务）并刷新设备卡徽标。"""
        env = environment_status()
        self._env_state = (env["tts_ready"], env["llama_ready"])
        self._resource_bar.set_env_state(*self._env_state)

    def _on_env_changed(self) -> None:
        """环境搭建结束（含取消/失败）：重新检测并刷新设备卡徽标。"""
        self._refresh_env_state()

    # ------------------------------------------------------------------ 任务窗口

    def _track_window(self, window: TaskWindow) -> None:
        """登记任务窗口，销毁时自动移除引用。"""
        self._task_windows.append(window)
        window.destroyed.connect(
            lambda _obj=None, w=window: self._task_windows.remove(w)
            if w in self._task_windows else None
        )

    def _open_new_task_window(self) -> None:
        """任务列表"＋"：新建空任务窗口。"""
        window = TaskWindow(self._task_manager, parent=self)
        self._track_window(window)
        window.show()

    def _activate_task_window(self, task_id: str) -> None:
        """双击任务行：聚焦已有任务窗口，或为终态任务重建窗口。"""
        window = self._task_manager.window_for(task_id)
        if window is not None:
            window.show()
            window.raise_()
            window.activateWindow()
            return
        # 窗口已销毁（任务终结后关闭过）：重建并绑定
        item = self._task_manager.get_task(task_id)
        if item is None:
            return
        window = TaskWindow(self._task_manager, parent=self)
        self._track_window(window)
        window.bind_task(task_id)
        window.show()

    # ------------------------------------------------------------------ 窗口关闭

    def closeEvent(self, event) -> None:
        """关闭主窗口：有未终结任务先确认中断，再停止队列与采样线程，销毁全部任务窗口。"""
        if self._task_manager.has_active_tasks() and not self._confirm_interrupt():
            event.ignore()
            return
        self._task_manager.shutdown()
        for window in list(self._task_windows):
            window.force_close()
        self._task_windows.clear()
        self._monitor.stop()
        super().closeEvent(event)

    def _confirm_interrupt(self) -> bool:
        """任务执行中的关闭确认：确认中断返回 True，取消关闭返回 False。"""
        box = QMessageBox(self)
        box.setWindowTitle(tr("main.close_confirm_title"))
        box.setText(tr("main.close_confirm_text"))
        confirm = box.addButton(tr("common.confirm"), QMessageBox.AcceptRole)
        box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is confirm

    # ------------------------------------------------------------------ 设置窗口

    def _open_settings_dialog(self) -> None:
        """打开统一设置窗口（运行环境 + 资源管理 + 界面语言）。"""
        dialog = SettingsDialog(self._manager, device=self._device, parent=self)
        dialog.language_changed.connect(self._on_language_changed)
        dialog.exec()

    def _on_language_changed(self, _code: str) -> None:
        """界面语言已切换：整体重建界面（任务列表从队列重新填充）。"""
        self._build_ui()
