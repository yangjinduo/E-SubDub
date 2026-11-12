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
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.config import config
from app.core.i18n import get_setting, set_setting, tr


def prompt_output_dir_if_empty(edit: QLineEdit, parent=None) -> str:
    """输出目录为空时提示并打开目录选择；返回最终目录（取消返回空串）。

    选定后回填输入框并立即持久化（tasks.json 顶层 output_dir 键，
    两个设置面板初始化时预填该值）；非空时原样返回并同步持久化
    （手动输入的目录同样记为"上次输出目录"）。
    """
    text = edit.text().strip()
    if text:
        set_setting("output_dir", text)
        return text
    QMessageBox.information(
        parent, tr("task.output_dir_title"), tr("task.output_dir_required")
    )
    # 浏览起始定位兜底：config.OUTPUT_DIR（存在时）
    start = str(config.OUTPUT_DIR) if config.OUTPUT_DIR.is_dir() else ""
    path = QFileDialog.getExistingDirectory(
        parent, tr("task.choose_output_dir"), start
    )
    if not path:
        return ""
    edit.setText(path)
    set_setting("output_dir", path)
    return path

# 内容语言选项：(语言代码, i18n key)。源语言含"自动检测"。
# ar（阿拉伯语）：翻译（Index-Translate-2B）与时长修正（Homura）均已
# 支持，与主力语言并列进入媒体任务
SOURCE_LANGUAGE_CODES = ["auto", "zh", "en", "ja", "ko", "fr", "de", "es", "ru", "ar"]

# IndexTTS-2.5 原生支持的语言（实测其余语种合成发音错误，不得开放）：
# 配音目标语言的译文要交给 IndexTTS 合成，必须限定在此清单内；
# 与文本转语音任务共享，保证"能选的就能正确发声"
TTS_NATIVE_LANG_CODES = ["zh", "en", "ja", "es", "ar"]

# 配音与字幕：目标语言 = TTS 原生语言（译文要合成配音）
DUBBING_TARGET_LANGUAGE_CODES = TTS_NATIVE_LANG_CODES

# 生成字幕：只翻译不合成，目标语言不受 TTS 限制，全语言开放
TARGET_LANGUAGE_CODES = ["zh", "en", "ja", "ko", "fr", "de", "es", "ru", "ar"]

# 文本转语音：文本语言（auto = 自动识别 + zhen 中英混合）
TTS_LANG_CODES = ["auto"] + TTS_NATIVE_LANG_CODES + ["zhen"]

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
    """媒体任务设置卡片（配音与字幕 / 生成字幕共用）。

    show_separate：是否显示"人声分离"开关（仅生成字幕窗口；配音任务
    恒分离，开关无意义故不显示，separate_enabled() 恒返回 True）。
    """

    # 人声分离开关切换信号（新状态；字幕窗口用于换装流程步骤链）
    separate_toggled = Signal(bool)

    def __init__(
        self,
        show_overlay: bool = True,
        show_separate: bool = False,
        parent=None,
    ) -> None:
        """show_overlay：是否显示"混音后叠加原背景音"开关（仅配音窗口）。

        show_separate：是否显示"人声分离"开关（仅生成字幕窗口）。
        """
        super().__init__(parent)
        self.setProperty("card", True)
        self._show_separate = show_separate

        title = QLabel(tr("task.title"))
        title.setObjectName("sectionTitle")

        # 第一行：源语言 / 目标语言 / 字幕形式（三组等宽铺满整行，两端对齐）
        self._source_combo = QComboBox()
        for code in SOURCE_LANGUAGE_CODES:
            self._source_combo.addItem(tr(f"lang.{code}"), code)

        self._target_combo = QComboBox()
        # 目标语言按任务窗口区分：配音（show_overlay=True）的译文要
        # 交给 IndexTTS 合成，与文本转语音任务同款语言清单（TTS 原生
        # 语种，保证"能选的就能正确发声"）；生成字幕只翻译不合成，
        # 全语言开放
        target_codes = (
            DUBBING_TARGET_LANGUAGE_CODES
            if show_overlay
            else TARGET_LANGUAGE_CODES
        )
        for code in target_codes:
            self._target_combo.addItem(tr(f"lang.{code}"), code)
        # 默认目标语言：英语（最常用）
        self._target_combo.setCurrentIndex(
            self._target_combo.findData("en")
        )

        self._subtitle_combo = QComboBox()
        for code, key in SUBTITLE_CHOICES:
            self._subtitle_combo.addItem(tr(key), code)
        # 默认字幕形式：双语字幕（目标语言在上，最常用）
        self._subtitle_combo.setCurrentIndex(
            self._subtitle_combo.findData(SUBTITLE_BILINGUAL_TARGET)
        )

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

        # 人声分离（仅生成字幕窗口显示，默认开启）：ASR 识别分离出的
        # 纯人声，背景音较大时字幕准确率更高；配音任务恒分离不显示
        self._separate_check = QCheckBox(tr("task.separate"))
        self._separate_check.setChecked(True)  # 默认开启
        self._separate_check.setToolTip(tr("task.separate.tooltip"))
        self._separate_check.setToolTipDuration(24 * 60 * 60 * 1000)
        self._separate_check.toggled.connect(
            lambda checked: self.separate_toggled.emit(checked)
        )
        if not show_separate:
            self._separate_check.hide()

        # 输出目录：无默认值（必填），预填上次使用的输出目录（持久化于
        # tasks.json 顶层 output_dir 键，可能为空），为空时 placeholder 提示
        self._output_edit = QLineEdit(str(get_setting("output_dir") or ""))
        self._output_edit.setPlaceholderText(tr("task.output_dir_placeholder"))
        self._browse_button = QPushButton("…")
        self._browse_button.setFixedWidth(44)
        self._browse_button.clicked.connect(self._browse_output_dir)
        output_label = QLabel(tr("task.output_dir"))
        output_label.setStyleSheet("color: #6B7280;")

        option_row = QHBoxLayout()
        option_row.setSpacing(12)
        if show_overlay:
            option_row.addWidget(self._overlay_original_check)
        if show_separate:
            option_row.addWidget(self._separate_check)
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

    def separate_enabled(self) -> bool:
        """人声分离开关状态（配音窗口无此开关，恒 True）。"""
        return self._separate_check.isChecked()

    def set_separate_enabled(self, enabled: bool) -> None:
        """恢复人声分离开关（绑定已有任务/语言切换重建时；不发射信号）。"""
        self._separate_check.blockSignals(True)
        self._separate_check.setChecked(bool(enabled))
        self._separate_check.blockSignals(False)

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
            "separate": self._separate_check.isChecked(),
            "output": self._output_edit.text(),
        }

    def load_state(self, state: dict) -> None:
        """恢复导出的设置（阻塞信号，避免触发联动）。"""
        if not state:
            return
        widgets = (
            self._source_combo, self._target_combo,
            self._subtitle_combo, self._overlay_original_check,
            self._separate_check,
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
            if "separate" in state:
                self._separate_check.setChecked(state["separate"])
            if "output" in state:
                self._output_edit.setText(state["output"])
        finally:
            for w in widgets:
                w.blockSignals(False)

    def set_selected_codes(
        self, source: str, target: str, subtitle: str,
        overlay_original: bool, output_dir: str,
        separate: bool = True,
    ) -> None:
        """按稳定代码恢复设置（任务窗口绑定已有任务时使用）。

        separate 仅生成字幕窗口使用（配音窗口开关隐藏，恢复无副作用）。
        """
        for combo, code in (
            (self._source_combo, source),
            (self._target_combo, target),
            (self._subtitle_combo, subtitle),
        ):
            index = combo.findData(code)
            if index >= 0:
                combo.setCurrentIndex(index)
        self._overlay_original_check.setChecked(overlay_original)
        # 阻塞信号：避免 toggled 触发步骤链换装，调用方随后统一
        # _on_separate_toggled + _restore_pipeline（防双重恢复）
        self._separate_check.blockSignals(True)
        self._separate_check.setChecked(separate)
        self._separate_check.blockSignals(False)
        self._output_edit.setText(output_dir)

    # ------------------------------------------------------------------ 交互

    def ensure_output_dir(self) -> str:
        """输出目录为空时提示并选择（任务开始前校验用）；取消返回空串。"""
        return prompt_output_dir_if_empty(self._output_edit, self)

    def _browse_output_dir(self) -> None:
        """浏览选择输出目录（选定即持久化为"上次输出目录"）。"""
        current = self.output_dir() or str(config.OUTPUT_DIR)
        path = QFileDialog.getExistingDirectory(self, tr("task.choose_output_dir"), current)
        if path:
            self._output_edit.setText(path)
            set_setting("output_dir", path)


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

        # 第二行：参考音频人声分离 / 生成 SRT 字幕开关 / 输出格式 / 输出目录
        # 参考人声分离（默认开启）：Demucs 去除参考中的背景杂音后再克隆；
        # 所选文件已带纯人声标记时由任务窗口自动取消勾选并禁用
        self._ref_separate_check = QCheckBox(tr("tts.ref_separate"))
        self._ref_separate_check.setChecked(True)
        self._ref_separate_check.setToolTip(tr("tts.ref_separate.tooltip"))
        self._ref_separate_check.setToolTipDuration(24 * 60 * 60 * 1000)
        # 纯人声文件强制禁用前的用户勾选记忆（恢复可勾选时还原）
        self._ref_separate_locked_state: bool | None = None

        self._srt_check = QCheckBox(tr("tts.generate_srt"))
        self._srt_check.setChecked(True)  # 默认生成字幕

        self._format_combo = QComboBox()
        for code, key in TTS_FORMATS:
            self._format_combo.addItem(tr(key), code)

        # 输出目录：无默认值（必填），预填上次使用的输出目录（可能为空）
        self._output_edit = QLineEdit(str(get_setting("output_dir") or ""))
        self._output_edit.setPlaceholderText(tr("task.output_dir_placeholder"))
        self._browse_button = QPushButton("…")
        self._browse_button.setFixedWidth(44)
        self._browse_button.clicked.connect(self._browse_output_dir)
        output_label = QLabel(tr("task.output_dir"))
        output_label.setStyleSheet("color: #6B7280;")
        format_label = QLabel(tr("tts.output_format"))
        format_label.setStyleSheet("color: #6B7280;")

        row2 = QHBoxLayout()
        row2.setSpacing(12)
        row2.addWidget(self._ref_separate_check)
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

    def ref_separate_enabled(self) -> bool:
        """参考音频人声分离开关状态（纯人声文件强制禁用时恒 False）。"""
        return self._ref_separate_check.isChecked()

    def set_ref_separate_for_pure_voice(self, pure: bool) -> None:
        """按所选参考是否带纯人声标记切换开关可用性（选中文件时调用）。

        pure=True：记忆当前勾选后取消勾选并禁用（防重复分离）；
        pure=False：恢复可用，并还原记忆的勾选状态（无记忆则保持现值）。
        """
        check = self._ref_separate_check
        if pure:
            if check.isEnabled():
                self._ref_separate_locked_state = check.isChecked()
            check.blockSignals(True)
            check.setChecked(False)
            check.blockSignals(False)
            check.setEnabled(False)
            check.setToolTip(tr("tts.ref_separate.pure_hint"))
            return
        check.setEnabled(True)
        check.setToolTip(tr("tts.ref_separate.tooltip"))
        if self._ref_separate_locked_state is not None:
            check.blockSignals(True)
            check.setChecked(self._ref_separate_locked_state)
            check.blockSignals(False)
            self._ref_separate_locked_state = None

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
            "ref_separate": self._ref_separate_check.isChecked(),
            "ref_separate_enabled_widget": self._ref_separate_check.isEnabled(),
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
            self._ref_separate_check,
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
            if "ref_separate" in state:
                self._ref_separate_check.setChecked(state["ref_separate"])
            if "ref_separate_enabled_widget" in state:
                # 纯人声文件的强制禁用随 state 一并还原（语言切换重建）
                self._ref_separate_check.setEnabled(
                    state["ref_separate_enabled_widget"]
                )
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
        ref_separate: bool = True,
    ) -> None:
        """按稳定代码恢复设置（任务窗口绑定已有 TTS 任务时使用）。

        ref_separate 为任务配置的参考人声分离开关；所选参考为纯人声
        文件时随后的 _set_ref_audio 会强制禁用并记忆该值。
        """
        self._name_edit.setText(task_name)
        for combo, code in ((self._lang_combo, lang), (self._format_combo, output_format)):
            index = combo.findData(code)
            if index >= 0:
                combo.setCurrentIndex(index)
        index = self._speed_combo.findData(round(float(speed), 1))
        if index >= 0:
            self._speed_combo.setCurrentIndex(index)
        self._srt_check.setChecked(bool(generate_srt))
        self._ref_separate_check.setChecked(bool(ref_separate))
        self._output_edit.setText(output_dir)

    # ------------------------------------------------------------------ 交互

    def ensure_output_dir(self) -> str:
        """输出目录为空时提示并选择（任务开始前校验用）；取消返回空串。"""
        return prompt_output_dir_if_empty(self._output_edit, self)

    def _browse_output_dir(self) -> None:
        """浏览选择输出目录（选定即持久化为"上次输出目录"）。"""
        current = self.output_dir() or str(config.OUTPUT_DIR)
        path = QFileDialog.getExistingDirectory(self, tr("task.choose_output_dir"), current)
        if path:
            self._output_edit.setText(path)
            set_setting("output_dir", path)
