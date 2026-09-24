"""任务设置区：源/目标语言、字幕形式（下拉）、配音开关、输出目录。

布局：
- 第一行：源语言 / 目标语言 / 字幕形式（三个同款下拉菜单）
- 第二行：配音开关 / 输出目录

语言选项内部携带稳定语言代码（itemData），供后续流水线模块使用；
界面文案统一走 i18n（tr），语言切换后由主窗口整体重建。
"""

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
)

from app.core.config import config
from app.core.i18n import tr

# 内容语言选项：(语言代码, i18n key)。源语言含"自动检测"
SOURCE_LANGUAGE_CODES = ["auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru"]
TARGET_LANGUAGE_CODES = ["zh", "en", "ja", "ko", "fr", "de", "es", "ru"]

# 字幕形式取值（稳定代码，供流水线使用）
SUBTITLE_SINGLE = "single"                       # 单语字幕
SUBTITLE_BILINGUAL_TARGET = "bilingual_target"   # 双语字幕（目标语言在上）
SUBTITLE_BILINGUAL_SOURCE = "bilingual_source"   # 双语字幕（源语言在上）
# 字幕形式下拉：(代码, i18n key)
SUBTITLE_CHOICES = [
    (SUBTITLE_SINGLE, "task.subtitle.single"),
    (SUBTITLE_BILINGUAL_TARGET, "task.subtitle.bilingual_target"),
    (SUBTITLE_BILINGUAL_SOURCE, "task.subtitle.bilingual_source"),
]


class TaskSettingsPanel(QFrame):
    """任务设置卡片。"""

    # 配音开关切换时发射
    dubbing_enabled_changed = Signal(bool)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)

        title = QLabel(tr("task.title"))
        title.setObjectName("sectionTitle")

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(14)

        # 第一行：源语言 / 目标语言 / 字幕形式（同款下拉菜单）
        source_label = QLabel(tr("task.source_language"))
        source_label.setStyleSheet("color: #6B7280;")
        self._source_combo = QComboBox()
        for code in SOURCE_LANGUAGE_CODES:
            self._source_combo.addItem(tr(f"lang.{code}"), code)

        target_label = QLabel(tr("task.target_language"))
        target_label.setStyleSheet("color: #6B7280;")
        self._target_combo = QComboBox()
        for code in TARGET_LANGUAGE_CODES:
            self._target_combo.addItem(tr(f"lang.{code}"), code)

        subtitle_label = QLabel(tr("task.subtitle_mode"))
        subtitle_label.setStyleSheet("color: #6B7280;")
        self._subtitle_combo = QComboBox()
        for code, key in SUBTITLE_CHOICES:
            self._subtitle_combo.addItem(tr(key), code)

        grid.addWidget(source_label, 0, 0, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self._source_combo, 0, 1)
        grid.addWidget(target_label, 0, 2, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self._target_combo, 0, 3)
        grid.addWidget(subtitle_label, 0, 4, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self._subtitle_combo, 0, 5)

        # 第二行：配音开关 / 输出目录
        dubbing_label = QLabel(tr("task.dubbing"))
        dubbing_label.setStyleSheet("color: #6B7280;")
        self._dubbing_check = QCheckBox(tr("task.dubbing_check"))
        self._dubbing_check.setChecked(True)  # 默认开启
        self._dubbing_check.toggled.connect(self.dubbing_enabled_changed.emit)
        grid.addWidget(dubbing_label, 1, 0, Qt.AlignmentFlag.AlignRight)
        grid.addWidget(self._dubbing_check, 1, 1)

        output_label = QLabel(tr("task.output_dir"))
        output_label.setStyleSheet("color: #6B7280;")
        output_row = QHBoxLayout()
        output_row.setSpacing(8)
        self._output_edit = QLineEdit(str(config.OUTPUT_DIR))
        self._browse_button = QPushButton("…")
        self._browse_button.setFixedWidth(44)
        self._browse_button.clicked.connect(self._browse_output_dir)
        output_row.addWidget(self._output_edit, 1)
        output_row.addWidget(self._browse_button)
        grid.addWidget(output_label, 1, 2, Qt.AlignmentFlag.AlignRight)
        grid.addLayout(output_row, 1, 3, 1, 3)

        # 输出目录输入框与浏览按钮高度对齐语言下拉菜单（文字不再被裁切）
        combo_height = self._source_combo.sizeHint().height()
        self._output_edit.setMinimumHeight(combo_height)
        self._browse_button.setMinimumHeight(combo_height)

        # 列拉伸：让语言下拉与输出目录填满剩余空间
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        grid.setColumnStretch(5, 1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addLayout(grid)

    # ------------------------------------------------------------------ 取值

    def source_language(self) -> str:
        """当前源语言显示文本（如"自动检测"）。"""
        return self._source_combo.currentText()

    def source_language_code(self) -> str:
        """当前源语言稳定代码（"auto"/"zh"/"en"/…，供流水线使用）。"""
        return self._source_combo.currentData()

    def target_language(self) -> str:
        """当前目标语言显示文本。"""
        return self._target_combo.currentText()

    def target_language_code(self) -> str:
        """当前目标语言稳定代码（"zh"/"en"/…，供流水线使用）。"""
        return self._target_combo.currentData()

    def subtitle_mode(self) -> str:
        """当前字幕形式（SUBTITLE_* 常量之一）。"""
        return self._subtitle_combo.currentData()

    def dubbing_enabled(self) -> bool:
        """配音开关状态。"""
        return self._dubbing_check.isChecked()

    def output_dir(self) -> str:
        """输出目录路径文本。"""
        return self._output_edit.text().strip()

    # ------------------------------------------------------------------ 状态保留

    def state(self) -> dict:
        """导出当前设置（语言切换重建 UI 时保留用户选择）。"""
        return {
            "source": self._source_combo.currentIndex(),
            "target": self._target_combo.currentIndex(),
            "subtitle": self._subtitle_combo.currentIndex(),
            "dubbing": self._dubbing_check.isChecked(),
            "output": self._output_edit.text(),
        }

    def load_state(self, state: dict) -> None:
        """恢复导出的设置（阻塞信号，避免触发联动）。"""
        if not state:
            return
        self._source_combo.blockSignals(True)
        self._target_combo.blockSignals(True)
        self._subtitle_combo.blockSignals(True)
        self._dubbing_check.blockSignals(True)
        try:
            if "source" in state:
                self._source_combo.setCurrentIndex(state["source"])
            if "target" in state:
                self._target_combo.setCurrentIndex(state["target"])
            if "subtitle" in state:
                self._subtitle_combo.setCurrentIndex(state["subtitle"])
            if "dubbing" in state:
                self._dubbing_check.setChecked(state["dubbing"])
            if "output" in state:
                self._output_edit.setText(state["output"])
        finally:
            self._source_combo.blockSignals(False)
            self._target_combo.blockSignals(False)
            self._subtitle_combo.blockSignals(False)
            self._dubbing_check.blockSignals(False)

    def set_selected_codes(
        self, source: str, target: str, subtitle: str,
        dubbing: bool, output_dir: str,
    ) -> None:
        """按稳定代码恢复设置（任务窗口绑定已有任务时使用）。"""
        for combo, code in (
            (self._source_combo, source),
            (self._target_combo, target),
            (self._subtitle_combo, subtitle),
        ):
            index = combo.findData(code)
            if index >= 0:
                combo.setCurrentIndex(index)
        self._dubbing_check.setChecked(dubbing)
        self._output_edit.setText(output_dir)

    # ------------------------------------------------------------------ 交互

    def _browse_output_dir(self) -> None:
        """浏览选择输出目录。"""
        current = self.output_dir() or str(config.OUTPUT_DIR)
        path = QFileDialog.getExistingDirectory(self, tr("task.choose_output_dir"), current)
        if path:
            self._output_edit.setText(path)
