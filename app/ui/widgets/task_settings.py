"""任务设置区：媒体任务与文本转语音任务各一个设置面板。

TaskSettingsPanel（媒体任务：配音与字幕 / 生成字幕）：
- 第一行：源语言 / 目标语言 / 字幕形式（三组“标签+下拉”等宽铺满整行）
- 第二行：叠加原背景音开关（仅配音窗口显示）/ 输出目录

TtsSettingsPanel（文本转语音任务）：
- 第一行：任务名 / 文本语言（自动识别+可选语种）/ 语速
- 第二行：生成 SRT 字幕开关 / 输出格式 / 输出目录

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

# 文本转语音：文本语言（auto = 自动识别 + zhen 中英混合）
TTS_LANG_CODES = ["auto", "zh", "en", "ja", "es", "ar", "zhen"]

# 文本转语音：语速档位（0.5-2.0，步进 0.1；1.0 正常）
TTS_SPEEDS = [round(0.5 + 0.1 * i, 1) for i in range(16)]

# 文本转语音：输出格式下拉 (代码, i18n key)
TTS_FORMATS = [
    ("wav", "tts.format.wav"),
    ("mp3", "tts.format.mp3"),
    ("m4a", "tts.format.m4a"),
    ("flac", "tts.format.flac"),
]

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
    """媒体任务设置卡片（配音与字幕 / 生成字幕共用）。"""

    def __init__(self, show_overlay: bool = True, parent=None) -> None:
        """show_overlay：是否显示"混音后叠加原背景音"开关（仅配音窗口）。"""
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

        # 第二行：叠加原背景音开关（仅配音窗口）/ 输出目录
        # 混音后叠加原背景音（默认开启）：混音完成后，每句配音（含背景音）
        # 按对白时间线覆盖回原声音频——对白时段播新配音，句间等其余时段
        # 保留原声，笑声、惊叹等语气词与现场氛围不丢失（仅配音任务生效）
        self._overlay_original_check = QCheckBox(tr("task.overlay_original"))
        self._overlay_original_check.setChecked(True)  # 默认开启
        self._overlay_original_check.setToolTip(tr("task.overlay_original.tooltip"))
        # TOOLTIP 不限时显示：用户看完前不自动消失，移开鼠标才隐藏
        self._overlay_original_check.setToolTipDuration(24 * 60 * 60 * 1000)
        if not show_overlay:
            self._overlay_original_check.hide()

        self._output_edit = QLineEdit(str(config.OUTPUT_DIR))
        self._browse_button = QPushButton("…")
        self._browse_button.setFixedWidth(44)
        self._browse_button.clicked.connect(self._browse_output_dir)
        output_label = QLabel(tr("task.output_dir"))
        output_label.setStyleSheet("color: #6B7280;")

        option_row = QHBoxLayout()
        option_row.setSpacing(12)
        if show_overlay:
            option_row.addWidget(self._overlay_original_check)
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
        """当前目标语言稳定代码（"zh"/"en"/…）。"""
        return self._target_combo.currentData()

    def subtitle_mode(self) -> str:
        """当前字幕形式（SUBTITLE_* 常量之一）。"""
        return self._subtitle_combo.currentData()

    def overlay_original_audio(self) -> bool:
        """混音后叠加原背景音开关状态（对白覆盖回原声，保留句间语气词）。"""
        return self._overlay_original_check.isChecked()

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
            "overlay_original": self._overlay_original_check.isChecked(),
            "output": self._output_edit.text(),
        }

    def load_state(self, state: dict) -> None:
        """恢复导出的设置（阻塞信号，避免触发联动）。"""
        if not state:
            return
        widgets = (
            self._source_combo, self._target_combo,
            self._subtitle_combo, self._overlay_original_check,
        )
        for w in widgets:
            w.blockSignals(True)
        try:
            if "source" in state:
                self._source_combo.setCurrentIndex(state["source"])
            if "target" in state:
                self._target_combo.setCurrentIndex(state["target"])
            if "subtitle" in state:
                self._subtitle_combo.setCurrentIndex(state["subtitle"])
            if "overlay_original" in state:
                self._overlay_original_check.setChecked(state["overlay_original"])
            if "output" in state:
                self._output_edit.setText(state["output"])
        finally:
            for w in widgets:
                w.blockSignals(False)

    def set_selected_codes(
        self, source: str, target: str, subtitle: str,
        overlay_original: bool, output_dir: str,
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
        self._overlay_original_check.setChecked(overlay_original)
        self._output_edit.setText(output_dir)

    # ------------------------------------------------------------------ 交互

    def _browse_output_dir(self) -> None:
        """浏览选择输出目录。"""
        current = self.output_dir() or str(config.OUTPUT_DIR)
        path = QFileDialog.getExistingDirectory(self, tr("task.choose_output_dir"), current)
        if path:
            self._output_edit.setText(path)


class TtsSettingsPanel(QFrame):
    """文本转语音任务设置卡片。

    语言下拉的"自动识别"项文案由任务窗口按检测结果动态更新
    （set_auto_lang_label："自动识别（德语）"式）。
    """

    # 语速值变化信号（值, 显示文本）
    speed_changed = Signal(float)
    # 语言选择变化信号（语言代码；多音字提示等随语言切换显隐）
    lang_changed = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)

        title = QLabel(tr("task.title"))
        title.setObjectName("sectionTitle")

        # 第一行：任务名 / 文本语言 / 语速
        self._name_edit = QLineEdit()
        self._name_edit.setPlaceholderText(tr("tts.task_name_hint"))

        self._lang_combo = QComboBox()
        for code in TTS_LANG_CODES:
            self._lang_combo.addItem(tr(f"lang.{code}"), code)
        self._lang_combo.currentIndexChanged.connect(
            lambda _i: self.lang_changed.emit(self.lang_code())
        )

        self._speed_combo = QComboBox()
        for value in TTS_SPEEDS:
            self._speed_combo.addItem(f"{value:.1f}×", value)
        self._speed_combo.setCurrentIndex(TTS_SPEEDS.index(1.0))
        self._speed_combo.currentIndexChanged.connect(
            lambda _i: self.speed_changed.emit(self.speed())
        )

        row1 = QHBoxLayout()
        row1.setSpacing(16)
        for label_text, widget, stretch in (
            (tr("tts.task_name"), self._name_edit, 2),
            (tr("tts.language"), self._lang_combo, 1),
            (tr("tts.speed"), self._speed_combo, 1),
        ):
            label = QLabel(label_text)
            label.setStyleSheet("color: #6B7280;")
            pair = QHBoxLayout()
            pair.setSpacing(8)
            pair.addWidget(label)
            pair.addWidget(widget, stretch)
            row1.addLayout(pair, stretch)

        # 第二行：生成 SRT 字幕开关 / 输出格式 / 输出目录
        self._srt_check = QCheckBox(tr("tts.generate_srt"))
        self._srt_check.setChecked(True)  # 默认生成字幕

        self._format_combo = QComboBox()
        for code, key in TTS_FORMATS:
            self._format_combo.addItem(tr(key), code)

        self._output_edit = QLineEdit(str(config.OUTPUT_DIR))
        self._browse_button = QPushButton("…")
        self._browse_button.setFixedWidth(44)
        self._browse_button.clicked.connect(self._browse_output_dir)
        output_label = QLabel(tr("task.output_dir"))
        output_label.setStyleSheet("color: #6B7280;")
        format_label = QLabel(tr("tts.output_format"))
        format_label.setStyleSheet("color: #6B7280;")

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        row2.addWidget(self._srt_check)
        row2.addStretch(1)
        format_pair = QHBoxLayout()
        format_pair.setSpacing(8)
        format_pair.addWidget(format_label)
        format_pair.addWidget(self._format_combo)
        row2.addLayout(format_pair)
        row2.addSpacing(12)
        row2.addWidget(output_label)
        row2.addWidget(self._output_edit, 2)
        row2.addWidget(self._browse_button)

        # 输出目录输入框与浏览按钮高度对齐下拉菜单（文字不被裁切）
        combo_height = self._lang_combo.sizeHint().height()
        self._output_edit.setMinimumHeight(combo_height)
        self._browse_button.setMinimumHeight(combo_height)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(14)
        layout.addWidget(title)
        layout.addLayout(row1)
        layout.addLayout(row2)

    # ------------------------------------------------------------------ 取值

    def task_name(self) -> str:
        """任务名（TTS 任务无媒体文件：命名/输出均以任务名为基础）。"""
        return self._name_edit.text().strip()

    def lang_code(self) -> str:
        """文本语言稳定代码（"auto"/"zh"/"en"/"ja"/"es"/"ar"/"zhen"）。"""
        return self._lang_combo.currentData()

    def speed(self) -> float:
        """语速（0.5-2.0，1.0 正常）。"""
        return float(self._speed_combo.currentData())

    def generate_srt(self) -> bool:
        """是否生成 SRT 字幕（时间轴按合成音频实际时长生成）。"""
        return self._srt_check.isChecked()

    def output_format(self) -> str:
        """输出音频格式代码（"wav"/"mp3"/"m4a"/"flac"）。"""
        return self._format_combo.currentData()

    def output_dir(self) -> str:
        """输出目录路径文本。"""
        return self._output_edit.text().strip()

    # ------------------------------------------------------------------ 状态保留

    def set_auto_lang_label(self, display: str) -> str:
        """更新"自动识别"下拉项的动态文案，返回更新后的完整文案。

        display 为空时恢复纯"自动识别"；非空时显示"自动识别（{语言名}）"。
        """
        text = (
            tr("tts.lang_auto_detected", lang=display)
            if display else tr("lang.auto")
        )
        self._lang_combo.setItemText(0, text)
        return text

    def set_task_name(self, name: str) -> None:
        """程序填充任务名（默认任务名/文件名联动，非用户手动编辑）。"""
        self._name_edit.setText(name)

    def set_task_name_enabled(self, enabled: bool) -> None:
        """任务名输入框可用性（多文件模式下任务名随文件名，禁用手输）。"""
        self._name_edit.setEnabled(enabled)
        self._name_edit.setToolTip(
            "" if enabled else tr("tts.name_follow_files")
        )

    def state(self) -> dict:
        """导出当前设置。"""
        return {
            "name": self._name_edit.text(),
            "lang": self._lang_combo.currentIndex(),
            "speed": self._speed_combo.currentIndex(),
            "srt": self._srt_check.isChecked(),
            "format": self._format_combo.currentIndex(),
            "output": self._output_edit.text(),
        }

    def load_state(self, state: dict) -> None:
        """恢复导出的设置（阻塞信号，避免触发联动）。"""
        if not state:
            return
        widgets = (
            self._name_edit, self._lang_combo,
            self._speed_combo, self._srt_check, self._format_combo,
        )
        for w in widgets:
            w.blockSignals(True)
        try:
            if "name" in state:
                self._name_edit.setText(state["name"])
            if "lang" in state:
                self._lang_combo.setCurrentIndex(state["lang"])
            if "speed" in state:
                self._speed_combo.setCurrentIndex(state["speed"])
            if "srt" in state:
                self._srt_check.setChecked(state["srt"])
            if "format" in state:
                self._format_combo.setCurrentIndex(state["format"])
            if "output" in state:
                self._output_edit.setText(state["output"])
        finally:
            for w in widgets:
                w.blockSignals(False)

    def set_selected_values(
        self, task_name: str, lang: str, speed: float,
        generate_srt: bool, output_format: str, output_dir: str,
    ) -> None:
        """按稳定代码恢复设置（任务窗口绑定已有 TTS 任务时使用）。"""
        self._name_edit.setText(task_name)
        for combo, code in ((self._lang_combo, lang), (self._format_combo, output_format)):
            index = combo.findData(code)
            if index >= 0:
                combo.setCurrentIndex(index)
        index = self._speed_combo.findData(round(float(speed), 1))
        if index >= 0:
            self._speed_combo.setCurrentIndex(index)
        self._srt_check.setChecked(bool(generate_srt))
        self._output_edit.setText(output_dir)

    # ------------------------------------------------------------------ 交互

    def _browse_output_dir(self) -> None:
        """浏览选择输出目录。"""
        current = self.output_dir() or str(config.OUTPUT_DIR)
        path = QFileDialog.getExistingDirectory(self, tr("task.choose_output_dir"), current)
        if path:
            self._output_edit.setText(path)
