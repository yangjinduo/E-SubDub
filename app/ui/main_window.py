"""主窗口：顶部资源状态栏 + 中部任务列表 + 底部系统监视栏。

任务配置与执行监控移入独立的任务窗口（点任务列表"＋"创建）；
本窗口只承担队列总览：任务状态/进度一览、双击打开任务窗口。
语言切换时通过 _build_ui() 整体重建（TaskManager 挂在主窗口本体，
队列与任务窗口不受重建影响）。
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMainWindow, QWidget, QVBoxLayout

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
        # 最新设备检测结果（语言切换重建界面时复用，不重复检测）
        self._device: DeviceInfo = device_info or detect_device_standalone()
        self._device_refreshed = False
        # 运行环境（TTS 虚拟环境 / LLAMA 服务）就绪状态，设备卡徽标用
        self._env_state: tuple[bool, bool] = (False, False)
        # 任务队列（挂在主窗口本体上：语言切换重建 central widget 不受影响）
        self._task_manager = TaskManager(parent=self)
        self._task_manager.device = self._device.device
        # 环境搭建完成后刷新设备卡徽标（设置窗口触发，窗口关闭后仍生效）
        get_env_setup_manager().environment_changed.connect(self._on_env_changed)
        # 打开中的任务窗口（含未开始的空窗口；应用退出时统一销毁）
        self._task_windows: list[TaskWindow] = []
        self._build_ui()

    # ------------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        """构建全部界面；语言切换时可重复调用（旧的 central widget 被自动销毁）。"""
        # 语言切换重建前停掉旧的监视采样线程
        if hasattr(self, "_monitor"):
            self._monitor.stop()

        self.setWindowTitle(tr("main.title"))
        self.setMinimumSize(1100, 750)  # 最小尺寸，窗口可自由缩放

        # ------------------------------ 顶部：资源状态栏 ------------------------------
        self._resource_bar = ResourceStatusBar(self._manager)
        self._resource_bar.set_device(self._device)
        self._refresh_env_state()
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

        # 界面显示后用 torch 做一次精确设备检测（导入 torch 需数秒，仅首次执行）
        if not self._device_refreshed:
            QTimer.singleShot(300, self._refresh_device)

    # ------------------------------------------------------------------ 设备

    def _refresh_device(self) -> None:
        """基于 torch 的精确设备检测，刷新设备卡显示与队列推理设备。"""
        self._device = detect_device()
        self._resource_bar.set_device(self._device)
        self._task_manager.device = self._device.device
        self._device_refreshed = True

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
        """关闭主窗口：停止队列与采样线程，销毁全部任务窗口。"""
        self._task_manager.shutdown()
        for window in list(self._task_windows):
            window.force_close()
        self._task_windows.clear()
        self._monitor.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------------ 设置窗口

    def _open_settings_dialog(self) -> None:
        """打开统一设置窗口（运行环境 + 资源管理 + 界面语言）。"""
        dialog = SettingsDialog(self._manager, device=self._device, parent=self)
        dialog.language_changed.connect(self._on_language_changed)
        dialog.exec()

    def _on_language_changed(self, _code: str) -> None:
        """界面语言已切换：整体重建界面（任务列表从队列重新填充）。"""
        self._build_ui()
