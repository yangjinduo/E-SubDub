"""任务设置区：源/目标语言、字幕形式（下拉）、配音开关、AI 校对开关、输出目录。

布局：
- 第一行：源语言 / 目标语言 / 字幕形式（三组“标签+下拉”等宽铺满整行，两端对齐）
- 第二行：配音开关 / AI 校对开关 / 输出目录

语言选项内部携带稳定语言代码（itemData），供后续流水线模块使用；
界面文案统一走 i18n（tr），语言切换后由主窗口整体重建。
"""

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
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

        # 第一行：源语言 / 目标语言 / 字幕形式（三组等宽铺满整行，两端对齐）
        self._source_combo = QComboBox()
        for code in SOURCE_LANGUAGE_CODES:
            self._source_combo.addItem(tr(f"lang.{code}"), code)

        self._target_combo = QComboBox()
        for code in TARGET_LANGUAGE_CODES:
            self._target_combo.addItem(tr(f"lang.{code}"), code)
        # 默认目标语言：英语（最常用）
        self._target_combo.setCurrentIndex(TARGET_LANGUAGE_CODES.index("en"))

        self._subtitle_combo = QComboBox()
        for code, key in SUBTITLE_CHOICES:
            self._subtitle_combo.addItem(tr(key), code)

        lang_row = QHBoxLayout()
        lang_row.setSpacing(16)
        for label_text, combo in (
            (tr("task.source_language"), self._source_combo),
            (tr("task.target_language"), self._target_combo),
            (tr("task.subtitle_mode"), self._subtitle_combo),
        ):
            label = QLabel(label_text)
            label.setStyleSheet("color: #6B7280;")
            pair = QHBoxLayout()
            pair.setSpacing(8)
            pair.addWidget(label)
            pair.addWidget(combo, 1)
            lang_row.addLayout(pair, 1)

        # 第二行：配音开关 / AI 校对开关 / 输出目录
        self._dubbing_check = QCheckBox(tr("task.dubbing_check"))
        self._dubbing_check.setChecked(True)  # 默认开启
        self._dubbing_check.toggled.connect(self.dubbing_enabled_changed.emit)

        # AI 校对（可选）：勾选后文本先经 LLM 校对再进入人工审核，
        # 未勾选时 ASR 结果直接进入人工审核
        self._ai_proofread_check = QCheckBox(tr("task.ai_proofread"))
        self._ai_proofread_check.setChecked(False)  # 默认关闭

        self._output_edit = QLineEdit(str(config.OUTPUT_DIR))
        self._browse_button = QPushButton("…")
        self._browse_button.setFixedWidth(44)
        self._browse_button.clicked.connect(self._browse_output_dir)
        output_label = QLabel(tr("task.output_dir"))
        output_label.setStyleSheet("color: #6B7280;")

        option_row = QHBoxLayout()
        option_row.setSpacing(12)
        option_row.addWidget(self._dubbing_check)
        option_row.addWidget(self._ai_proofread_check)
        option_row.addStretch(1)
        option_row.addWidget(output_label)
        option_row.addWidget(self._output_edit, 2)
        option_row.addWidget(self._browse_button)

        # 输出目录输入框与浏览按钮高度对齐语言下拉菜单（文字不再被裁切）
        combo_height = self._source_combo.sizeHint().height()
        self._output_edit.setMinimumHeight(combo_height)
        self._browse_button.setMinimumHeight(combo_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addLayout(lang_row)
        layout.addLayout(option_row)

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

    def ai_proofread_enabled(self) -> bool:
        """AI 校对开关状态（文本先经 LLM 校对再人工审核）。"""
        return self._ai_proofread_check.isChecked()

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
            "ai_proofread": self._ai_proofread_check.isChecked(),
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
        self._ai_proofread_check.blockSignals(True)
        try:
            if "source" in state:
                self._source_combo.setCurrentIndex(state["source"])
            if "target" in state:
                self._target_combo.setCurrentIndex(state["target"])
            if "subtitle" in state:
                self._subtitle_combo.setCurrentIndex(state["subtitle"])
            if "dubbing" in state:
                self._dubbing_check.setChecked(state["dubbing"])
            if "ai_proofread" in state:
                self._ai_proofread_check.setChecked(state["ai_proofread"])
            if "output" in state:
                self._output_edit.setText(state["output"])
        finally:
            self._source_combo.blockSignals(False)
            self._target_combo.blockSignals(False)
            self._subtitle_combo.blockSignals(False)
            self._dubbing_check.blockSignals(False)
            self._ai_proofread_check.blockSignals(False)

    def set_selected_codes(
        self, source: str, target: str, subtitle: str,
        dubbing: bool, ai_proofread: bool, output_dir: str,
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
        self._ai_proofread_check.setChecked(ai_proofread)
        self._output_edit.setText(output_dir)

    # ------------------------------------------------------------------ 交互

    def _browse_output_dir(self) -> None:
        """浏览选择输出目录。"""
        current = self.output_dir() or str(config.OUTPUT_DIR)
        path = QFileDialog.getExistingDirectory(self, tr("task.choose_output_dir"), current)
        if path:
            self._output_edit.setText(path)
