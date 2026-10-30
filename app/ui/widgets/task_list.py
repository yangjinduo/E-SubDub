# -*- coding: utf-8 -*-
"""主窗口任务列表：六列表格（序号/任务名称/类别/状态/进度/删除）+ 新建/删除任务按钮。

- 行数据与 TaskManager 队列同步（入列/移除整表重建，状态/进度单行刷新）；
- 任务名称格底层绘制进度条（不遮挡文字），进度列显示百分比文本；
- 支持多选（Shift 连选 / Ctrl 多选 / 鼠标框选），多选任务可经头部
  "删除任务"按钮批量删除；每行删除列有独立删除按钮；删除前均弹确认；
- 双击行发射 task_activated（打开/聚焦该任务的任务窗口）；
- 右键菜单按任务状态提供：取消排队 / 重试 / 清理中间文件 / 打开输出文件夹 /
  移除记录。
"""

import logging

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
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
from app.core.task_manager import TaskItem, TaskManager, TaskState
from app.core.tts_runner import TtsTaskConfig
from app.ui.style import tokens

# 启动期里程碑日志：冻结态无控制台，启动日志是唯一诊断出口
# （冻结构建曾出现 TaskListPanel 构造期原生访问违例：根因是 Nuitka
# 4.3 与 PySide6 互操作缺陷，构建须钉 nuitka==4.2.2，见 build.ps1）
logger = logging.getLogger(__name__)

# 任务状态 → i18n key（RUNNING 不在此表：_status_text 单独处理，显示当前流程名）
_STATE_KEYS = {
    TaskState.QUEUED: "queue.state.queued",
    TaskState.CANCELLING: "queue.state.cancelling",
    TaskState.DONE: "queue.state.done",
    TaskState.FAILED: "queue.state.failed",
    TaskState.CANCELLED: "queue.state.cancelled",
    TaskState.INTERRUPTED: "queue.state.interrupted",
}


def _status_text(item: TaskItem) -> str:
    """任务状态显示文本（运行中附带当前流程名）。"""
    if item.state == TaskState.RUNNING:
        stage = tr(f"step.{item.current_stage}") if item.current_stage else ""
        return f"{stage}…" if stage else tr("queue.state.running")
    return tr(_STATE_KEYS.get(item.state, "queue.state.queued"))


def _category_text(item: TaskItem) -> str:
    """任务类别显示文本：文本转语音 / 配音与字幕 / 生成字幕（按配置类型判定）。"""
    if isinstance(item.config, TtsTaskConfig):
        return tr("queue.cat.tts")
    if item.config.dubbing_enabled:
        return tr("queue.cat.dub")
    return tr("queue.cat.sub")


class _TaskNameCell(QWidget):
    """任务名称单元格：底层整格进度条 + 上层完整任务名（文字不被遮挡）。

    进度条铺满单元格作背景，进度填充浅蓝色从左向右推进；任务名居中
    绘制在进度条之上，超宽时中间省略，悬停可看全名。
    """

    def __init__(self, name: str, parent=None) -> None:
        super().__init__(parent)
        self._name = name
        self._progress = 0
        self.setToolTip(name)
        # 自绘颜色按当前主题取令牌（深浅色都清晰）
        c = tokens()
        self._track = QColor(c["cell_track_bg"])     # 进度轨道
        self._fill = QColor(c["cell_track_fill"])    # 进度填充
        self._text = QColor(c["row_text"])           # 任务名文字

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
        painter.setBrush(self._track)
        painter.drawRoundedRect(track, radius, radius)

        # 进度填充（至少一个圆角宽度，避免小进度时形状异常）
        if self._progress > 0:
            fill = QRectF(track)
            fill.setWidth(max(track.width() * self._progress / 100.0, track.height()))
            painter.setBrush(self._fill)
            painter.drawRoundedRect(fill, radius, radius)

        # 上层任务名（居中，超宽中间省略）
        painter.setPen(self._text)
        metrics = QFontMetrics(self.font())
        text = metrics.elidedText(
            self._name, Qt.TextElideMode.ElideMiddle, int(track.width()) - 8,
        )
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, text)


class TaskListPanel(QFrame):
    """任务列表面板（主窗口中部）。"""

    # 点击"＋ 新建任务"按钮（kind: dub 配音与字幕 / sub 生成字幕 / tts 文本转语音）
    add_requested = Signal(str)
    task_activated = Signal(str)   # 双击任务行（task_id）

    # 三个新建按钮：(kind, i18n key, QSS objectName)；配色见 style.py
    _ADD_BUTTONS: list[tuple[str, str, str]] = [
        ("dub", "queue.add_dub", "addDubButton"),
        ("sub", "queue.add_sub", "addSubButton"),
        ("tts", "queue.add_tts", "addTtsButton"),
    ]

    def __init__(self, manager: TaskManager, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._manager = manager
        self._row_by_id: dict[str, int] = {}
        # 行删除按钮（task_id → 按钮，随整表重建刷新）
        self._delete_buttons: dict[str, QPushButton] = {}
        logger.info("任务列表：基础属性初始化完成")

        title = QLabel(tr("queue.title"))
        title.setObjectName("sectionTitle")
        self._delete_button = QPushButton(tr("queue.delete"))
        self._delete_button.setObjectName("dangerButton")
        self._delete_button.setEnabled(False)
        self._delete_button.clicked.connect(self._delete_selected_tasks)
        # 继续 / 停止双态按钮：无活动任务时"继续"（批量重入列上次未完成
        # 的任务），有活动任务时"停止"（停止整个队列：当前任务取消且
        # 排队任务全部取消，与任务窗口内仅停止单个任务的按钮语义不同）
        self._resume_button = QPushButton(tr("queue.resume"))
        self._resume_button.setObjectName("continueButton")
        self._resume_button.clicked.connect(self._on_queue_button_clicked)
        logger.info("任务列表：标题与操作按钮创建完成")

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.addWidget(title)
        header.addStretch(1)
        for kind, key, object_name in self._ADD_BUTTONS:
            button = QPushButton(tr(key))
            button.setObjectName(object_name)
            logger.info("任务列表：新建按钮就绪（%s），开始连接点击信号", kind)
            button.clicked.connect(
                lambda _checked=False, k=kind: self.add_requested.emit(k)
            )
            header.addWidget(button)
        header.addWidget(self._delete_button)
        header.addWidget(self._resume_button)

        self._table = QTableWidget(0, 6)
        self._table.setObjectName("taskTable")
        logger.info("任务列表：表格已创建")
        self._table.setHorizontalHeaderLabels([
            tr("queue.col.index"), tr("queue.col.name"),
            tr("queue.col.category"), tr("queue.col.status"),
            tr("queue.col.progress"), tr("queue.col.delete"),
        ])
        logger.info("任务列表：表头标签已设置")
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 扩展选择：支持 Shift 连选、Ctrl 多选、鼠标拖拽框选
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        logger.info("任务列表：表格行为配置完成")
        self._table.customContextMenuRequested.connect(self._show_menu)
        self._table.cellDoubleClicked.connect(self._on_double_clicked)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        logger.info("任务列表：表格信号已连接")
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setDefaultSectionSize(38)
        logger.info("任务列表：列宽模式配置完成")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)
        layout.addLayout(header)
        layout.addWidget(self._table, 1)
        logger.info("任务列表：布局总装完成")

        # 队列事件 → 表格刷新
        manager.task_added.connect(lambda _tid: self._rebuild())
        manager.task_removed.connect(lambda _tid: self._rebuild())
        logger.info("任务列表：增删信号已连接")
        manager.task_state_changed.connect(self._on_state_changed)
        manager.task_progress_changed.connect(self._on_progress_changed)
        # 流程步骤变化 → 状态列显示当前流程名（提取音频…/翻译…）
        manager.task_step_changed.connect(self._on_step_changed)
        logger.info("任务列表：状态/进度信号已连接")
        # 队列结构/状态变化 → "继续执行"按钮可用性
        manager.task_added.connect(lambda _tid: self._update_resume_button())
        manager.task_removed.connect(lambda _tid: self._update_resume_button())
        manager.task_state_changed.connect(
            lambda _tid: self._update_resume_button()
        )
        logger.info("任务列表：双态按钮信号已连接")
        self._update_resume_button()
        logger.info("任务列表：双态按钮已刷新")
        self._rebuild()
        logger.info("任务列表：整表重建完成")

    # ------------------------------------------------------------------ 行操作

    def _rebuild(self) -> None:
        """按队列顺序整表重建（任务增删时调用，任务量小无性能问题）。"""
        tasks = self._manager.tasks()
        self._table.setRowCount(0)
        self._row_by_id.clear()
        self._delete_buttons.clear()
        for row, item in enumerate(tasks):
            self._table.insertRow(row)
            self._row_by_id[item.id] = row

            index = QTableWidgetItem(str(row + 1))
            index.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name = _TaskNameCell(item.display_name)
            name.set_progress(item.progress)
            category = QTableWidgetItem(_category_text(item))
            category.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            status = QTableWidgetItem(_status_text(item))
            status.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            progress = QTableWidgetItem(f"{item.progress}%")
            progress.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            delete = self._build_row_delete_button(item)

            self._table.setItem(row, 0, index)
            self._table.setCellWidget(row, 1, name)
            self._table.setItem(row, 2, category)
            self._table.setItem(row, 3, status)
            self._table.setItem(row, 4, progress)
            self._table.setCellWidget(row, 5, delete)
        self._on_selection_changed()

    def _build_row_delete_button(self, item: TaskItem) -> QPushButton:
        """构造行删除按钮（运行中/取消中禁用）。"""
        button = QPushButton(tr("common.delete"))
        button.setObjectName("rowDeleteButton")
        button.setFixedHeight(28)
        button.setEnabled(item.state not in (
            TaskState.RUNNING, TaskState.CANCELLING,
        ))
        button.clicked.connect(
            lambda _checked=False, tid=item.id: self._delete_task_by_id(tid)
        )
        self._delete_buttons[item.id] = button
        return button

    def _update_row(self, item: TaskItem) -> None:
        """刷新单行的状态、进度与删除按钮可用性。"""
        row = self._row_by_id.get(item.id)
        if row is None:
            return
        status = self._table.item(row, 3)
        if status is not None:
            status.setText(_status_text(item))
        button = self._delete_buttons.get(item.id)
        if button is not None:
            button.setEnabled(item.state not in (
                TaskState.RUNNING, TaskState.CANCELLING,
            ))
        self._set_row_progress(row, item.progress)

    def _set_row_progress(self, row: int, value: int) -> None:
        """刷新任务名格的进度条背景与进度列的百分比文本。"""
        name_cell = self._table.cellWidget(row, 1)
        if isinstance(name_cell, _TaskNameCell):
            name_cell.set_progress(value)
        progress = self._table.item(row, 4)
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

    # ------------------------------------------------------------------ 继续 / 停止

    def _update_resume_button(self) -> None:
        """双态按钮：有活动任务显示"停止"（红色危险样式，可点）；否则
        显示"继续"（浅绿），无可继续任务（已中断/失败/已取消）时禁用。"""
        stop = self._manager.has_active_tasks()
        name = "dangerButton" if stop else "continueButton"
        if self._resume_button.objectName() != name:
            self._resume_button.setObjectName(name)
            self._resume_button.style().unpolish(self._resume_button)
            self._resume_button.style().polish(self._resume_button)
        self._resume_button.setText(
            tr("queue.stop" if stop else "queue.resume")
        )
        self._resume_button.setEnabled(
            stop or self._manager.has_resumable_tasks()
        )

    def _on_queue_button_clicked(self) -> None:
        """按钮点击：活动任务存在 → 停止整个队列；否则走继续确认流程。"""
        if self._manager.has_active_tasks():
            self._manager.stop_queue()
            return
        self._resume_unfinished()

    def _resume_unfinished(self) -> None:
        """批量继续未完成任务（已中断/失败/已取消），确认后重新入列。"""
        count = sum(
            1 for item in self._manager.tasks()
            if item.state in (
                TaskState.INTERRUPTED, TaskState.FAILED, TaskState.CANCELLED,
            )
        )
        if count == 0:
            QMessageBox.information(self, tr("queue.resume_title"),
                                    tr("queue.resume_none"))
            return
        answer = QMessageBox.question(
            self, tr("queue.resume_title"),
            tr("queue.resume_confirm", count=count),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        resumed = self._manager.resume_unfinished()
        if resumed:
            QMessageBox.information(
                self, tr("queue.resume_title"),
                tr("queue.resume_started", count=resumed),
            )

    # ------------------------------------------------------------------ 删除

    def _selected_items(self) -> list[TaskItem]:
        """当前选中的任务（按行序）。"""
        rows = sorted({
            index.row() for index in self._table.selectionModel().selectedRows()
        })
        items = []
        for row in rows:
            item = self._task_at(row)
            if item is not None:
                items.append(item)
        return items

    def _on_selection_changed(self) -> None:
        """头部"删除任务"按钮随选中状态启用/禁用。"""
        self._delete_button.setEnabled(
            any(
                item.state not in (TaskState.RUNNING, TaskState.CANCELLING)
                for item in self._selected_items()
            )
        )

    def _delete_task_by_id(self, task_id: str) -> None:
        """删除单个任务（行删除按钮入口，删除前确认）。"""
        item = self._manager.get_task(task_id)
        if item is None:
            return
        if item.state in (TaskState.RUNNING, TaskState.CANCELLING):
            QMessageBox.information(self, tr("queue.delete"), tr("queue.delete_running"))
            return
        if not self._confirm_delete([item]):
            return
        self._manager.delete_task(task_id)

    def _delete_selected_tasks(self) -> None:
        """批量删除选中任务（头部按钮入口，删除前确认）。"""
        items = self._selected_items()
        if not items:
            return
        deletable = [
            item for item in items
            if item.state not in (TaskState.RUNNING, TaskState.CANCELLING)
        ]
        running = [item for item in items if item not in deletable]
        if not deletable:
            QMessageBox.information(self, tr("queue.delete"), tr("queue.delete_running"))
            return
        if not self._confirm_delete(deletable):
            return
        for item in deletable:
            self._manager.delete_task(item.id)
        if running:
            QMessageBox.information(
                self, tr("queue.delete"),
                tr("queue.delete_running_skipped", count=len(running)),
            )

    def _confirm_delete(self, items: list[TaskItem]) -> bool:
        """删除确认对话框，确认返回 True。"""
        box = QMessageBox(self)
        box.setWindowTitle(tr("queue.confirm_delete_title"))
        box.setText(tr(
            "queue.confirm_delete_tasks",
            count=len(items),
            names="\n".join(f"· {item.display_name}" for item in items),
        ))
        delete_button = box.addButton(tr("common.delete"), QMessageBox.DestructiveRole)
        box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        box.exec()
        return box.clickedButton() is delete_button

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
        if item.state in (
            TaskState.FAILED, TaskState.CANCELLED, TaskState.INTERRUPTED,
        ):
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
