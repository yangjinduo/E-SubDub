"""底部流程进度区：步骤链 + 总进度条 + 日志框。

步骤使用稳定 ID（供流程引擎调用），显示文案按当前界面语言生成；
三种任务窗口共用本面板，各自传入步骤列表（配音与字幕 10 步 /
生成字幕 6 步 / 文本转语音 3 步）。步骤完成后标签可点击
（发射 stage_clicked），用于打开对应流程的结果查看窗口。
"""

from PySide6.QtCore import QDateTime, Qt, Signal
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
# "proofread"（文本审核）= 人工审核（审核动作并入该步骤）；
# "tts_review"（配音复核）= TTS 合成后的复核暂停点；
# 序号由列表顺序自动生成
PIPELINE_STEPS: list[tuple[str, str]] = [
    ("extract", "step.extract"),
    ("separate", "step.separate"),
    ("asr", "step.asr"),
    ("proofread", "step.proofread"),
    ("translate", "step.translate"),
    ("review_translation", "step.review_translation"),
    ("tts", "step.tts"),
    ("tts_review", "step.tts_review"),
    ("mix", "step.mix"),
    ("mux", "step.mux"),
]

# 生成字幕任务（无配音链路）的步骤：末步封装输出把 SRT 归档到 10_封装输出
SUBTITLE_PIPELINE_STEPS: list[tuple[str, str]] = [
    ("extract", "step.extract"),
    ("asr", "step.asr"),
    ("proofread", "step.proofread"),
    ("translate", "step.translate"),
    ("review_translation", "step.review_translation"),
    ("mux", "step.mux"),
]

# 文本转语音任务的步骤：末步封装输出 = 拼接 + 转码 + SRT，成品直落 03_封装输出
TTS_PIPELINE_STEPS: list[tuple[str, str]] = [
    ("tts_text", "step.tts_text"),
    ("tts_synth", "step.tts_synth"),
    ("tts_mux", "step.mux"),
]

# 步骤状态：等待 / 进行中 / 完成
STEP_PENDING = "pending"
STEP_ACTIVE = "active"
STEP_DONE = "done"


class StepChip(QLabel):
    """单个流程步骤标签，通过 stepState 动态属性切换样式。

    步骤完成后（done 态）可点击，发射 clicked 信号用于打开结果查看窗口。
    """

    clicked = Signal()

    def __init__(self, text: str, parent=None) -> None:
        super().__init__(text, parent)
        self._state = STEP_PENDING
        self._clickable = False
        self.setProperty("stepState", STEP_PENDING)

    def set_state(self, state: str) -> None:
        """更新步骤状态（pending / active / done / disabled）。"""
        if state == self._state:
            return
        self._state = state
        self._clickable = state == STEP_DONE
        self.setProperty("stepState", state)
        self.setCursor(
            Qt.CursorShape.PointingHandCursor if self._clickable
            else Qt.CursorShape.ArrowCursor
        )
        self.style().unpolish(self)
        self.style().polish(self)

    def mouseReleaseEvent(self, event) -> None:
        if (
            event.button() == Qt.MouseButton.LeftButton
            and self._clickable
            and self.rect().contains(event.position().toPoint())
        ):
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class PipelinePanel(QFrame):
    """底部流程进度面板。"""

    # 点击已完成的流程步骤（参数为步骤 ID），用于打开结果查看窗口
    stage_clicked = Signal(str)

    def __init__(self, steps: list | None = None, parent=None) -> None:
        """steps：本窗口的流程步骤列表 [(step_id, i18n key)]，默认配音与字幕全量步骤。"""
        super().__init__(parent)
        self.setProperty("card", True)

        title = QLabel(tr("pipeline.title"))
        title.setObjectName("sectionTitle")

        # 步骤链：步骤标签带序号（与流程子夹序号对应），之间以箭头分隔
        step_list = steps if steps is not None else PIPELINE_STEPS
        self._chips: dict[str, StepChip] = {}
        steps_row = QHBoxLayout()
        steps_row.setContentsMargins(0, 0, 0, 0)
        steps_row.setSpacing(4)
        for index, (step_id, key) in enumerate(step_list):
            if index > 0:
                arrow = QLabel("→")
                arrow.setObjectName("stepArrow")
                steps_row.addWidget(arrow, 0)
            chip = StepChip(f"{index + 1:02d} {tr(key)}")
            chip.clicked.connect(
                lambda sid=step_id: self.stage_clicked.emit(sid)
            )
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
