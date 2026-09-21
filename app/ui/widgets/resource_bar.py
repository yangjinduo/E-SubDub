"""顶部资源状态栏：五张资源卡片 + 统一设置按钮 + 推理设备角标。

设置按钮不再分散在各卡片上，而是位于所有卡片右侧，点击后打开统一设置
窗口（查看全部资源路径 / 状态，并执行删除与下载）。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from app.core.hardware import DeviceInfo
from app.core.i18n import tr
from app.resources.model import ResourceInfo, ResourceManager, ResourceStatus


def _feature_label(info: ResourceInfo) -> str:
    """功能名按资源 key 取当前界面语言文案（模型名等专有名词不翻译）。"""
    return tr(f"resource.{info.key}.feature")


class ResourceCard(QFrame):
    """单张资源卡片：上行功能名 + 状态徽标，下行模型名。"""

    def __init__(self, info: ResourceInfo, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("resourceCard")
        self._key = info.key

        # 上行：功能名 + 状态徽标
        self._feature_label = QLabel()
        self._feature_label.setObjectName("resourceFeatureName")

        self._badge = QLabel()
        self._badge.setObjectName("notReadyBadge")

        title_row = QHBoxLayout()
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.setSpacing(8)
        title_row.addWidget(self._feature_label)
        title_row.addStretch(1)
        title_row.addWidget(self._badge)

        # 下行：模型名
        self._model_label = QLabel(info.model_name)
        self._model_label.setObjectName("resourceModelName")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)
        layout.addLayout(title_row)
        layout.addWidget(self._model_label)

        self.setFixedHeight(64)
        self.update_info(info)

    def update_info(self, info: ResourceInfo) -> None:
        """按最新资源信息刷新卡片内容与徽标。"""
        self._feature_label.setText(_feature_label(info))
        self._model_label.setText(info.model_name)

        if info.status is ResourceStatus.READY:
            self._badge.setText(tr("common.ready"))
            self._badge.setObjectName("readyBadge")
        else:
            self._badge.setText(tr("common.not_ready"))
            self._badge.setObjectName("notReadyBadge")
        # objectName 变化后需重新应用样式
        self._badge.style().unpolish(self._badge)
        self._badge.style().polish(self._badge)
        self._badge.adjustSize()


class ResourceStatusBar(QFrame):
    """顶部资源状态栏：横向五张卡片 + 统一设置按钮 + 当前推理设备角标。"""

    # 右侧统一设置按钮被点击（无参数，打开统一设置窗口）
    settings_requested = Signal()

    def __init__(self, manager: ResourceManager, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._cards: dict[str, ResourceCard] = {}

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)

        # 五张资源卡片
        for info in manager.resources():
            card = ResourceCard(info)
            self._cards[info.key] = card
            layout.addWidget(card, 1)

        layout.addStretch(0)

        # 统一设置按钮（所有资源卡片的右侧）
        self._settings_button = QPushButton(tr("common.settings"))
        self._settings_button.setObjectName("cardSettingButton")
        self._settings_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._settings_button.setFixedHeight(32)
        self._settings_button.clicked.connect(self.settings_requested.emit)
        layout.addWidget(self._settings_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addSpacing(8)

        # 角落：当前推理设备
        device_column = QVBoxLayout()
        device_column.setContentsMargins(0, 0, 4, 0)
        device_column.setSpacing(2)
        self._device_title = QLabel(tr("bar.device_title"))
        self._device_title.setObjectName("deviceLabelTitle")
        self._device_title.setAlignment(Qt.AlignmentFlag.AlignRight)
        self._device_value = QLabel()
        self._device_value.setObjectName("deviceLabelValue")
        self._device_value.setAlignment(Qt.AlignmentFlag.AlignRight)
        device_column.addWidget(self._device_title)
        device_column.addWidget(self._device_value)
        layout.addLayout(device_column)

        # 订阅管理器状态变化
        self._manager.resource_status_changed.connect(self._on_status_changed)

    def _on_status_changed(self, key: str) -> None:
        """资源状态变化时刷新对应卡片。"""
        card = self._cards.get(key)
        if card is not None:
            card.update_info(self._manager.get(key))

    def set_device(self, device: DeviceInfo) -> None:
        """更新角落的推理设备显示。"""
        self._device_value.setText(tr("bar.device_value", value=device.description))
        # 慢速设备提示色（红），快速设备为常规色
        slow_color = "#DC2626" if device.is_slow else "#059669"
        self._device_value.setStyleSheet(f"color: {slow_color};")
