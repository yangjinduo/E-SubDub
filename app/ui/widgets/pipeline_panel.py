"""底部流程进度区：步骤链 + 总进度条 + 日志框。

步骤使用稳定 ID（供流程引擎调用），显示文案按当前界面语言生成；
配音关闭（仅字幕模式）时置灰的步骤：人声分离 / 配音生成 / 混音 / 封装输出。
"""

from PySide6.QtCore import QDateTime
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QProgressBar,
    QVBoxLayout,
)

from app.core.i18n import tr

# 处理流程步骤（顺序固定）：(步骤 ID, i18n key)
PIPELINE_STEPS: list[tuple[str, str]] = [
    ("extract", "step.extract"),
    ("separate", "step.separate"),
    ("asr", "step.asr"),
    ("proofread", "step.proofread"),
    ("review", "step.review"),
    ("translate", "step.translate"),
    ("review_translation", "step.review_translation"),
    ("tts", "step.tts"),
    ("mix", "step.mix"),
    ("mux", "step.mux"),
]

# 仅字幕模式（配音关闭）时需要置灰的步骤 ID：
# 不生成配音就不需要人声分离，也不需要 TTS / 混音 / MKV 封装
DUBBING_STEP_IDS: set[str] = {"separate", "tts", "mix", "mux"}

# 步骤状态：等待 / 进行中 / 完成 / 已禁用
STEP_PENDING = "pending"
STEP_ACTIVE = "active"
STEP_DONE = "done"
STEP_DISABLED = "disabled"


class StepChip(QLabel):
    """单个流程步骤标签，通过 stepState 动态属性切换样式。"""

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self._state = STEP_PENDING
        self.setProperty("stepState", STEP_PENDING)

    def set_state(self, state: str) -> None:
        """更新步骤状态（pending / active / done / disabled）。"""
        if state == self._state:
            return
        self._state = state
        self.setProperty("stepState", state)
        self.style().unpolish(self)
        self.style().polish(self)


class PipelinePanel(QFrame):
    """底部流程进度面板。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)

        title = QLabel(tr("pipeline.title"))
        title.setObjectName("sectionTitle")

        # 步骤链：步骤标签之间以箭头分隔
        self._chips: dict[str, StepChip] = {}
        steps_row = QHBoxLayout()
        steps_row.setContentsMargins(0, 0, 0, 0)
        steps_row.setSpacing(4)
        for index, (step_id, key) in enumerate(PIPELINE_STEPS):
            if index > 0:
                arrow = QLabel("→")
                arrow.setObjectName("stepArrow")
                steps_row.addWidget(arrow, 0)
            chip = StepChip(tr(key))
            self._chips[step_id] = chip
            steps_row.addWidget(chip)

        # 总进度条
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(12)
        progress_caption = QLabel(tr("pipeline.total_progress"))
        progress_caption.setStyleSheet("color: #6B7280;")
        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setFormat("%p%")
        self._progress_bar.setTextVisible(False)
        progress_row.addWidget(progress_caption)
        progress_row.addWidget(self._progress_bar, 1)

        # 日志框
        self._log_box = QPlainTextEdit()
        self._log_box.setObjectName("logBox")
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumBlockCount(2000)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addWidget(title)
        layout.addLayout(steps_row)
        layout.addLayout(progress_row)
        layout.addWidget(self._log_box, 1)

    # ------------------------------------------------------------------ 对外接口

    def set_dubbing_enabled(self, enabled: bool) -> None:
        """配音关闭（仅字幕模式）时置灰配音相关步骤，开启时恢复等待态。"""
        for step_id in DUBBING_STEP_IDS:
            chip = self._chips.get(step_id)
            if chip is not None:
                chip.set_state(STEP_PENDING if enabled else STEP_DISABLED)

    def set_step_state(self, step_id: str, state: str) -> None:
        """设置指定步骤的状态（供流程引擎调用，参数为步骤 ID）。"""
        chip = self._chips.get(step_id)
        if chip is not None:
            chip.set_state(state)

    def set_progress(self, value: int) -> None:
        """设置总进度（0-100）。"""
        self._progress_bar.setValue(int(value))

    def log(self, message: str) -> None:
        """追加一行带时间戳的日志。"""
        timestamp = QDateTime.currentDateTime().toString("HH:mm:ss")
        self._log_box.appendPlainText(f"[{timestamp}] {message}")

    def log_text(self) -> str:
        """当前全部日志文本（语言切换重建 UI 时保留日志）。"""
        return self._log_box.toPlainText()

    def restore_log(self, text: str) -> None:
        """恢复此前导出的日志文本。"""
        if text:
            self._log_box.setPlainText(text)
