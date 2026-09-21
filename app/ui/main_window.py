"""主窗口：组装资源状态栏、导入区、任务设置区、流程进度区。

界面文案统一走 i18n（tr）；语言切换时通过 _build_ui() 整体重建界面，
并保留用户已导入的文件、任务设置与日志内容。
"""

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.hardware import DeviceInfo, detect_device, detect_device_standalone
from app.core.i18n import tr
from app.resources.model import get_resource_manager
from app.ui.dialogs.resource_dialog import SettingsDialog
from app.ui.widgets.import_area import ImportArea
from app.ui.widgets.pipeline_panel import PipelinePanel
from app.ui.widgets.resource_bar import ResourceStatusBar
from app.ui.widgets.task_settings import TaskSettingsPanel


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
        self._build_ui()

    # ------------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        """构建全部界面；语言切换时可重复调用（旧的 central widget 被自动销毁）。"""
        # 重建前保留用户状态（首次构建时无旧界面）
        previous = self._capture_state()

        self.setWindowTitle(tr("main.title"))
        self.setMinimumSize(1100, 750)  # 最小尺寸，窗口可自由缩放

        # ------------------------------ 顶部：资源状态栏 ------------------------------
        self._resource_bar = ResourceStatusBar(self._manager)
        self._resource_bar.set_device(self._device)
        self._resource_bar.settings_requested.connect(self._open_settings_dialog)

        # ------------------------------ 中部：导入区 ------------------------------
        self._import_area = ImportArea()
        self._import_area.file_imported.connect(self._on_file_imported)

        # ------------------------------ 任务设置区 ------------------------------
        self._task_settings = TaskSettingsPanel()
        if previous is not None:
            self._task_settings.load_state(previous["task_state"])

        # ------------------------------ 底部：流程进度区 ------------------------------
        self._pipeline_panel = PipelinePanel()
        if previous is not None and previous["log_text"]:
            self._pipeline_panel.restore_log(previous["log_text"])
        else:
            ready_count = sum(1 for r in self._manager.resources() if r.status.is_ready)
            total_count = len(self._manager.resources())
            self._pipeline_panel.log(
                tr("main.log.init", n=ready_count, total=total_count)
            )
            self._pipeline_panel.log(
                tr("main.log.device_initial", desc=self._device.description)
            )

        # 恢复配音开关对流程面板的影响（load_state 期间信号被阻塞）
        self._pipeline_panel.set_dubbing_enabled(self._task_settings.dubbing_enabled())
        self._task_settings.dubbing_enabled_changed.connect(self._on_dubbing_toggled)

        # 底部主操作按钮
        self._start_button = QPushButton(tr("main.start"))
        self._start_button.setObjectName("primaryButton")
        self._start_button.clicked.connect(self._on_start_clicked)
        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(0, 0, 0, 0)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self._start_button)

        # ------------------------------ 总装 ------------------------------
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._resource_bar)
        layout.addWidget(self._import_area, 1)
        layout.addWidget(self._task_settings)
        layout.addWidget(self._pipeline_panel, 2)
        layout.addLayout(bottom_bar)
        self.setCentralWidget(central)

        # 恢复此前导入的文件（会重新探测时长并记录日志）
        if previous is not None and previous["imported_file"]:
            self._import_area.set_file(previous["imported_file"])

        # 界面显示后用 torch 做一次精确设备检测（导入 torch 需数秒，仅首次执行）
        if not self._device_refreshed:
            QTimer.singleShot(300, self._refresh_device)

    def _capture_state(self) -> dict | None:
        """重建前收集旧界面状态；首次构建（无旧界面）返回 None。"""
        if not hasattr(self, "_task_settings"):
            return None
        return {
            "task_state": self._task_settings.state(),
            "log_text": self._pipeline_panel.log_text(),
            "imported_file": self._import_area.current_file,
        }

    # ------------------------------------------------------------------ 设备

    def _refresh_device(self) -> None:
        """基于 torch 的精确设备检测，刷新角标显示。"""
        self._device = detect_device()
        self._resource_bar.set_device(self._device)
        self._pipeline_panel.log(tr("main.log.device_done", desc=self._device.description))
        self._device_refreshed = True

    # ------------------------------------------------------------------ 事件处理

    def _on_file_imported(self, path: str) -> None:
        """导入文件后记录日志。"""
        from pathlib import Path

        self._pipeline_panel.log(tr("main.log.imported", name=Path(path).name))

    def _on_dubbing_toggled(self, enabled: bool) -> None:
        """配音开关切换：同步置灰/恢复流程面板的配音相关步骤。"""
        self._pipeline_panel.set_dubbing_enabled(enabled)
        self._pipeline_panel.log(
            tr("main.log.dubbing_on") if enabled else tr("main.log.dubbing_off")
        )

    def _on_start_clicked(self) -> None:
        """开始处理按钮（流程引擎待接入，仅做框架提示）。"""
        if self._import_area.current_file is None:
            self._pipeline_panel.log(tr("main.log.no_file"))
            return
        settings_desc = tr(
            "main.log.task_settings",
            src=self._task_settings.source_language(),
            dst=self._task_settings.target_language(),
        )
        self._pipeline_panel.log(tr("main.log.engine_pending", settings=settings_desc))

    # ------------------------------------------------------------------ 设置窗口

    def _open_settings_dialog(self) -> None:
        """打开统一设置窗口（资源管理 + 界面语言）。"""
        dialog = SettingsDialog(self._manager, self)
        # 用 lambda 转发日志：语言切换重建后仍指向最新的日志面板
        dialog.log_requested.connect(lambda message: self._pipeline_panel.log(message))
        dialog.language_changed.connect(self._on_language_changed)
        dialog.exec()

    def _on_language_changed(self, _code: str) -> None:
        """界面语言已切换：整体重建界面（用户状态自动保留）。"""
        self._build_ui()
