# -*- coding: utf-8 -*-
"""主窗口任务列表：四列表格（序号/任务名称/状态/进度）+ 新建任务按钮。

- 行数据与 TaskManager 队列同步（入列/移除整表重建，状态/进度单行刷新）；
- 任务名称格底层绘制进度条（不遮挡文字），进度列显示百分比文本；
- 双击行发射 task_activated（打开/聚焦该任务的任务窗口）；
- 右键菜单按任务状态提供：取消排队 / 重试 / 清理中间文件 / 打开输出文件夹 /
  移除记录。
"""

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtCore import QUrl
from PySide6.QtGui import QColor, QFontMetrics, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
from app.core.task_manager import TaskItem, TaskManager, TaskState

# 任务状态 → i18n key
_STATE_KEYS = {
    TaskState.QUEUED: "queue.state.queued",
    TaskState.RUNNING: "queue.state.running",
    TaskState.CANCELLING: "queue.state.cancelling",
    TaskState.DONE: "queue.state.done",
    TaskState.FAILED: "queue.state.failed",
    TaskState.CANCELLED: "queue.state.cancelled",
}


def _status_text(item: TaskItem) -> str:
    """任务状态显示文本（运行中附带当前流程名）。"""
    if item.state == TaskState.RUNNING:
        stage = tr(f"step.{item.current_stage}") if item.current_stage else ""
        return f"{stage}…" if stage else tr("queue.state.running")
    return tr(_STATE_KEYS.get(item.state, "queue.state.queued"))


class _TaskNameCell(QWidget):
    """任务名称单元格：底层整格进度条 + 上层完整任务名（文字不被遮挡）。

    进度条铺满单元格作背景，进度填充浅蓝色从左向右推进；任务名居中
    绘制在进度条之上，超宽时中间省略，悬停可看全名。
    """

    _TRACK = QColor("#EEF2F7")    # 进度轨道
    _FILL = QColor("#BFDBFE")     # 进度填充
    _TEXT = QColor("#1F2937")     # 任务名文字

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self._progress = 0
        self.setToolTip(name)

    def set_name(self, name: str) -> None:
        if name != self._name:
            self._name = name
            self.setToolTip(name)
            self.update()

    def set_progress(self, value: int) -> None:
        value = max(0, min(100, int(value)))
        if value != self._progress:
            self._progress = value
            self.update()

    def sizeHint(self):
        metrics = QFontMetrics(self.font())
        return metrics.size(0, self._name) + QSize(24, 18)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        # 底层轨道（整格圆角矩形，四周留 1px 呼吸空间）
        track = QRectF(1.0, 1.0, self.width() - 2.0, self.height() - 2.0)
        radius = track.height() / 2.0
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._TRACK)
        painter.drawRoundedRect(track, radius, radius)

        # 进度填充（至少一个圆角宽度，避免小进度时形状异常）
        if self._progress > 0:
            fill = QRectF(track)
            fill.setWidth(max(track.width() * self._progress / 100.0, track.height()))
            painter.setBrush(self._FILL)
            painter.drawRoundedRect(fill, radius, radius)

        # 上层任务名（居中，超宽中间省略）
        painter.setPen(self._TEXT)
        metrics = QFontMetrics(self.font())
        text = metrics.elidedText(
            self._name, Qt.TextElideMode.ElideMiddle, int(track.width()) - 8,
        )
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)


class TaskListPanel(QFrame):
    """任务列表面板（主窗口中部）。"""

    add_requested = Signal()       # 点击"＋ 新建任务"
    task_activated = Signal(str)   # 双击任务行（task_id）

    def __init__(self, manager: TaskManager, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._manager = manager
        self._row_by_id: dict[str, int] = {}

        title = QLabel(tr("queue.title"))
        title.setObjectName("sectionTitle")
        self._add_button = QPushButton(tr("queue.add"))
        self._add_button.setObjectName("addTaskButton")
        self._add_button.clicked.connect(self.add_requested.emit)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(title)
        header.addStretch(1)
        header.addWidget(self._add_button)

        self._table = QTableWidget(0, 4)
        self._table.setObjectName("taskTable")
        self._table.setHorizontalHeaderLabels([
            tr("queue.col.index"), tr("queue.col.name"),
            tr("queue.col.status"), tr("queue.col.progress"),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._show_menu)
        self._table.cellDoubleClicked.connect(self._on_double_clicked)
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self._table, 1)

        # 队列事件 → 表格刷新
        manager.task_added.connect(lambda _tid: self._rebuild())
        manager.task_removed.connect(lambda _tid: self._rebuild())
        manager.task_state_changed.connect(self._on_state_changed)
        manager.task_progress_changed.connect(self._on_progress_changed)
        # 流程步骤变化 → 状态列显示当前流程名（提取音频…/翻译…）
        manager.task_step_changed.connect(self._on_step_changed)
        self._rebuild()

    # ------------------------------------------------------------------ 行操作

    def _rebuild(self) -> None:
        """按队列顺序整表重建（任务增删时调用，任务量小无性能问题）。"""
        tasks = self._manager.tasks()
        self._table.setRowCount(0)
        self._row_by_id.clear()
        for row, item in enumerate(tasks):
            self._table.insertRow(row)
            self._row_by_id[item.id] = row

            index = QTableWidgetItem(str(row + 1))
            index.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name = _TaskNameCell(item.display_name)
            name.set_progress(item.progress)
            status = QTableWidgetItem(_status_text(item))
            status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            progress = QTableWidgetItem(f"{item.progress}%")
            progress.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            self._table.setItem(row, 0, index)
            self._table.setCellWidget(row, 1, name)
            self._table.setItem(row, 2, status)
            self._table.setItem(row, 3, progress)

    def _update_row(self, item: TaskItem) -> None:
        """刷新单行的状态与进度。"""
        row = self._row_by_id.get(item.id)
        if row is None:
            return
        status = self._table.item(row, 2)
        if status is not None:
            status.setText(_status_text(item))
        self._set_row_progress(row, item.progress)

    def _set_row_progress(self, row: int, value: int) -> None:
        """刷新任务名格的进度条背景与进度列的百分比文本。"""
        name_cell = self._table.cellWidget(row, 1)
        if isinstance(name_cell, _TaskNameCell):
            name_cell.set_progress(value)
        progress = self._table.item(row, 3)
        if progress is not None:
            progress.setText(f"{value}%")

    def _on_state_changed(self, task_id: str) -> None:
        item = self._manager.get_task(task_id)
        if item is not None:
            self._update_row(item)

    def _on_step_changed(self, task_id: str, _step_id: str, _state: str) -> None:
        """流程步骤切换时刷新状态列（运行中显示当前流程名）。"""
        item = self._manager.get_task(task_id)
        if item is not None:
            self._update_row(item)

    def _on_progress_changed(self, task_id: str, value: int) -> None:
        row = self._row_by_id.get(task_id)
        if row is not None:
            self._set_row_progress(row, value)

    # ------------------------------------------------------------------ 交互

    def _task_at(self, row: int) -> TaskItem | None:
        for task_id, r in self._row_by_id.items():
            if r == row:
                return self._manager.get_task(task_id)
        return None

    def _on_double_clicked(self, row: int, _col: int) -> None:
        item = self._task_at(row)
        if item is not None:
            self.task_activated.emit(item.id)

    def _show_menu(self, pos) -> None:
        """右键菜单：按任务状态显示可用操作。"""
        row = self._table.rowAt(pos.y())
        if row < 0:
            return
        item = self._task_at(row)
        if item is None:
            return
        self._table.selectRow(row)

        menu = QMenu(self)
        if item.state == TaskState.QUEUED:
            menu.addAction(tr("queue.menu.cancel"), lambda: self._manager.cancel(item.id))
        if item.state in (TaskState.FAILED, TaskState.CANCELLED):
            menu.addAction(tr("queue.menu.retry"), lambda: self._manager.retry(item.id))
        if TaskState.is_terminal(item.state):
            menu.addAction(
                tr("queue.menu.cleanup"),
                lambda: self._manager.cleanup(item.id),
            )
        menu.addAction(
            tr("queue.menu.open_folder"),
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(item.workspace.dir))),
        )
        if TaskState.is_terminal(item.state):
            menu.addAction(tr("queue.menu.remove"), lambda: self._manager.remove(item.id))
        menu.exec(self._table.viewport().mapToGlobal(pos))
