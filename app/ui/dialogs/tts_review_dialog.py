# -*- coding: utf-8 -*-
"""TTS 配音结果复核对话框。

显示全部配音句子：试听生成的配音、修改译文、即时重生成不满意的句子
（点击"重新生成此句"或批量按钮后由 regen_hook 在后台逐句排队执行，
生成中的句子显示"生成中…"、其余排队句显示"等待重生成"，完成后可
再次试听或重生成）；伸缩比异常（needs_review）的句子数值以红色粗体
标出。原文/译文列自动换行、行高随内容自适应；底部提供连续试听
（从当前句一路播放到最后一句）。Segment 采用鸭子类型，与
app.core.segment.Segment 完全兼容。
"""

from __future__ import annotations

import os
import threading
from collections import deque

from PySide6.QtCore import QEvent, QRect, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
from app.ui.style import table_qss, tokens
from app.ui.widgets.audio_player import AudioPlayerBar

# 行高自适应：最小行高与原文/译文换行内容上下的留白
_MIN_ROW_HEIGHT = 42
_CELL_V_PAD = 8
# 译文编辑框（QPlainTextEdit）边框 + 文档边距等固定附加高度
_EDITOR_CHROME = 12


def _fmt_time(seconds: float) -> str:
    """秒 → "分:秒.十分位"（如 83.46 → "01:23.4"）。"""
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


def _fmt_ratio(seg) -> str:
    """伸缩比显示：如 "1.45x"；无 stretch_ratio 属性或非法值时显示 "-"。"""
    ratio = getattr(seg, "stretch_ratio", None)
    if ratio in (None, ""):
        return "-"
    try:
        return f"{float(ratio):.2f}x"
    except (TypeError, ValueError):
        return "-"


def _ratio_item(seg) -> QTableWidgetItem:
    """伸缩比单元格：异常（needs_review）句以红色粗体显示。"""
    item = QTableWidgetItem(_fmt_ratio(seg))
    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
    if getattr(seg, "needs_review", False):
        item.setForeground(QColor(tokens()["danger_text"]))
        font = item.font()
        font.setBold(True)
        item.setFont(font)
    return item


class TtsReviewDialog(QDialog):
    """TTS 配音复核对话框。

    显示全部配音句子；译文编辑即时回写（get_edited_segments 返回传入的
    原列表）。重生成在对话框内即时完成（regen_hook 后台逐句执行），
    确认后流水线直接进入下一步，无需外层再批量重生成。
    """

    _regen_started = Signal(int)                # 句子开始生成（行号）
    _regen_finished = Signal(int, object, str)  # 句子生成完成（行号, 伸缩比|None, 错误）
    _regen_progress = Signal(int, float)       # 句子生成进度（行号, 0~1）
    _regen_queue_done = Signal()                # 队列排空（或被取消）

    def __init__(self, segments: list, parent=None, regen_hook=None) -> None:
        super().__init__(parent)
        self._segments = segments  # 全部配音句子列表，就地修改译文
        self._regen_hook = regen_hook  # 即时重生成回调：行号 → 伸缩比
        self._listen_buttons: list[QPushButton] = []
        self._regen_buttons: list[QPushButton] = []
        self._editors: list[QPlainTextEdit] = []
        self._continuous_playing = False  # 连续试听进行中
        self._continuous_index = -1  # 连续试听当前播放行

        # 重生成队列：_regen_pending 由锁保护（worker 线程消费）；
        # _regen_states/_regen_active 仅主线程访问（UI 状态）
        self._regen_pending: "deque[int]" = deque()
        self._regen_lock = threading.Lock()
        self._regen_states: dict[int, str] = {}  # 行号 → waiting / running
        self._regen_thread: threading.Thread | None = None
        self._regen_active = False
        self._regen_stop = False

        self._regen_started.connect(self._on_regen_started)
        self._regen_finished.connect(self._on_regen_finished)
        self._regen_progress.connect(self._on_regen_progress)
        self._regen_queue_done.connect(self._on_regen_queue_done)

        self.setWindowTitle(tr("tts_review.title"))
        self.resize(1020, 660)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题
        title = QLabel(tr("tts_review.heading"))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # 主表格
        self._table = QTableWidget(len(self._segments), 8, self)
        self._table.setHorizontalHeaderLabels(
            [
                tr("tts_review.column_index"),
                tr("tts_review.column_time"),
                tr("tts_review.column_speaker"),
                tr("tts_review.column_source"),
                tr("tts_review.column_target"),
                tr("tts_review.column_ratio"),
                tr("tts_review.column_listen"),
                tr("tts_review.column_regen"),
            ]
        )
        self._table.setStyleSheet(table_qss())
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_MIN_ROW_HEIGHT)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(True)  # 原文/译文列随列宽自动换行
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 46), (1, 130), (2, 70), (5, 70), (6, 70), (7, 110)):
            self._table.setColumnWidth(col, width)
        layout.addWidget(self._table, stretch=1)

        # 列宽变化（拖拽或窗口缩放）后重算各行的换行行高；拖拽过程中
        # sectionResized 触发频繁，用单发定时器合并
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(40)
        self._height_timer.timeout.connect(self._update_all_row_heights)
        header.sectionResized.connect(lambda *_: self._height_timer.start())

        # 底部试听条
        self._player = AudioPlayerBar(self)
        self._player.finished.connect(self._on_player_finished)
        layout.addWidget(self._player)

        # 连续试听行：与上方试听条同款布局（试听按钮 + 进度滑块 + 计数标签）
        continuous_row = QHBoxLayout()
        continuous_row.setContentsMargins(0, 0, 0, 0)
        continuous_row.setSpacing(10)
        self._continuous_button = QPushButton(tr("tts_review.continuous_listen"))
        self._continuous_button.setFixedWidth(72)  # 与试听按钮同宽
        # 72px 宽度下全局按钮样式的 18px 水平内边距放不下四字文案，
        # 仅收窄本按钮内边距，其余观感（底色/边框/圆角/高度）不变
        self._continuous_button.setStyleSheet("QPushButton { padding: 6px 8px; }")
        self._continuous_button.clicked.connect(self._on_continuous_clicked)
        self._continuous_slider = QSlider(Qt.Orientation.Horizontal)
        self._continuous_slider.setRange(0, max(len(self._segments) - 1, 0))
        self._continuous_slider.setValue(0)
        self._continuous_slider.setEnabled(False)  # 连续试听未开始时禁用
        self._continuous_slider.sliderReleased.connect(self._on_continuous_slider_released)
        self._continuous_count_label = QLabel(f"0 / {len(self._segments)}")
        self._continuous_count_label.setStyleSheet("color: #6B7280; font-size: 12px;")
        self._continuous_count_label.setMinimumWidth(110)
        continuous_row.addWidget(self._continuous_button)
        continuous_row.addWidget(self._continuous_slider, stretch=1)
        continuous_row.addWidget(self._continuous_count_label)
        layout.addLayout(continuous_row)

        # 统计与重生成操作行（位于取消/确认上一行，左对齐）
        action_row = QHBoxLayout()
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #6B7280;")
        action_row.addWidget(self._stats_label)
        self._regen_abnormal_button = QPushButton(tr("tts_review.regenerate_all"))
        self._regen_abnormal_button.setObjectName("cardSettingButton")
        self._regen_abnormal_button.clicked.connect(self._on_select_abnormal)
        action_row.addWidget(self._regen_abnormal_button)
        self._regen_selected_button = QPushButton(tr("tts_review.regenerate_selected"))
        self._regen_selected_button.setObjectName("cardSettingButton")
        self._regen_selected_button.clicked.connect(self._on_select_selected)
        action_row.addWidget(self._regen_selected_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)

        # 按钮行
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._cancel_button = QPushButton(tr("common.cancel"))
        self._cancel_button.clicked.connect(self.reject)
        self._ok_button = QPushButton(tr("common.confirm"))
        self._ok_button.setObjectName("primaryButton")
        self._ok_button.clicked.connect(self.accept)
        button_row.addWidget(self._cancel_button)
        button_row.addWidget(self._ok_button)
        layout.addLayout(button_row)

        self._fill_rows()
        self._refresh_stats()
        # 首次布局完成后按最终列宽校准各行高度（Stretch 列宽此时才确定）
        QTimer.singleShot(0, self._update_all_row_heights)

    # -------------------------------------------------------------- 公开接口

    def get_regenerate_indices(self) -> "list[int]":
        """兼容旧接口：重生成已改为对话框内即时完成，恒返回空列表。"""
        return []

    def get_edited_segments(self) -> list:
        """返回包含译文修改的分段列表（即传入的原列表，已就地更新）。"""
        return self._segments

    def done(self, result: int) -> None:
        """关闭前释放播放器持有的配音文件句柄（重生成会覆盖同名 wav）。"""
        self._continuous_playing = False  # 防止释放播放器后仍推进连续试听
        self._player.release()
        super().done(result)

    def accept(self) -> None:
        """确认进入下一步；重生成进行中时忽略点击（等待队列排空）。"""
        if self._regen_active:
            return
        super().accept()

    def reject(self) -> None:
        """取消：队列运行中先请求停止，当前句完成后自动关闭。"""
        if self._regen_active:
            self._regen_stop = True
            self._cancel_button.setEnabled(False)
            return
        super().reject()

    def eventFilter(self, obj, event) -> bool:
        """译文编辑框：拦截回车键，保持单句译文单行（不插入换行）。"""
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            return True
        return super().eventFilter(obj, event)

    # -------------------------------------------------------------- 内部构建

    def _fill_rows(self) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        for row, seg in enumerate(self._segments):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, 0, index_item)
            self._table.setItem(
                row, 1,
                QTableWidgetItem(
                    f"{_fmt_time(float(seg.start))} - {_fmt_time(float(seg.end))}"
                ),
            )
            self._table.setItem(row, 2, QTableWidgetItem(str(seg.spk)))
            # 原文列：与译文对照，方便修改过长的译文
            self._table.setItem(
                row, 3, QTableWidgetItem(str(getattr(seg, "text", "") or ""))
            )

            # 译文编辑框：自动换行、无滚动条；内容行数变化时行高自适应
            edit = QPlainTextEdit(str(seg.target_text))
            edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
            edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
            edit.setTabChangesFocus(True)
            edit.installEventFilter(self)  # 拦截回车，避免译文插入换行
            edit.textChanged.connect(lambda r=row: self._on_target_changed(r))
            edit_holder = QWidget()
            holder_layout = QVBoxLayout(edit_holder)
            holder_layout.setContentsMargins(2, 2, 2, 2)
            holder_layout.setSpacing(0)
            holder_layout.addWidget(edit, 0, Qt.AlignmentFlag.AlignVCenter)
            self._editors.append(edit)
            self._table.setCellWidget(row, 4, edit_holder)

            self._table.setItem(row, 5, _ratio_item(seg))

            # 试听按钮：tts_audio_path 为空或文件不存在时禁用。
            # 点击时按行号实时读取当前路径——重生成后文件名随译文
            # 变化（旧文件已被移除），闭包捕获旧路径会变成死链
            path = str(getattr(seg, "tts_audio_path", "") or "")
            listen_button = QPushButton(tr("tts_review.listen"))
            listen_button.setObjectName("cardSettingButton")
            listen_button.setEnabled(bool(path) and os.path.isfile(path))
            listen_button.clicked.connect(
                lambda _=False, r=row: self._on_listen_clicked(r)
            )
            self._listen_buttons.append(listen_button)
            listen_holder = QWidget()
            listen_layout = QHBoxLayout(listen_holder)
            listen_layout.setContentsMargins(6, 2, 6, 2)
            listen_layout.addWidget(listen_button)
            self._table.setCellWidget(row, 6, listen_holder)

            # 重新生成按钮：点击后即时排队重生成（生成期间显示状态）
            regen_button = QPushButton(tr("tts_review.regenerate_this"))
            regen_button.setObjectName("cardSettingButton")
            regen_button.clicked.connect(
                lambda _=False, r=row: self._on_regen_clicked(r)
            )
            self._regen_buttons.append(regen_button)
            self._table.setCellWidget(row, 7, regen_button)

    def _refresh_stats(self) -> None:
        """刷新底部统计标签（总数 / 异常数 / 待重生成数）。"""
        total = len(self._segments)
        abnormal = sum(1 for seg in self._segments if getattr(seg, "needs_review", False))
        pending = len(self._regen_states)
        self._stats_label.setText(
            tr("tts_review.stats", total=total, abnormal=abnormal, pending=pending)
        )

    # -------------------------------------------------------------- 内部槽

    def _on_target_changed(self, row: int) -> None:
        """译文编辑：即时回写 Segment，并按换行行数刷新行高。"""
        edit = self._editors[row]
        self._segments[row].target_text = edit.toPlainText()
        self._update_row_height(row)

    def _on_regen_clicked(self, row: int) -> None:
        """单句"重新生成此句"：排队后保持当前选中行与焦点不变。

        排队会立刻禁用被点击的按钮，Qt 会把焦点转移给下一行的译文
        编辑框，观感上像选中跳到了下一句；这里显式把焦点交还表格，
        并在选中行被连带改变时恢复原选中行。
        """
        current = self._table.currentRow()
        self._enqueue_regen([row])
        self._table.setFocus()
        if current >= 0 and self._table.currentRow() != current:
            self._table.setCurrentCell(current, max(self._table.currentColumn(), 0))

    def _on_select_abnormal(self) -> None:
        """批量按钮：把全部伸缩比异常句加入重生成队列。"""
        self._enqueue_regen([
            r for r, seg in enumerate(self._segments)
            if getattr(seg, "needs_review", False)
        ])

    def _on_select_selected(self) -> None:
        """批量按钮：把表格中当前选中的行（支持多选）加入重生成队列。"""
        selection = self._table.selectionModel()
        if selection is None:
            return
        self._enqueue_regen([index.row() for index in selection.selectedRows()])

    def _on_listen_clicked(self, row: int) -> None:
        """试听该句生成的配音（每次点击按当前 tts_audio_path 实时取文件，
        重生成后新文件名也能正确链接）。单句试听会中断连续试听。"""
        self._stop_continuous()
        seg = self._segments[row]
        path = str(getattr(seg, "tts_audio_path", "") or "")
        if path and os.path.isfile(path):
            self._player.set_source(path)
            self._player.play()

    # -------------------------------------------------------------- 行高自适应

    def _wrapped_height(self, text: str, width: int) -> int:
        """估算文本按给定宽度自动换行后所需的高度（像素）。"""
        fm = QFontMetrics(self._table.font())
        avail = max(width - 16, 30)  # 扣除单元格左右留白
        rect = fm.boundingRect(QRect(0, 0, avail, 100000), Qt.TextFlag.TextWordWrap, text)
        return rect.height()

    def _update_row_height(self, row: int) -> None:
        """按原文/译文换行后的高度调整行高，并同步译文编辑框高度。"""
        seg = self._segments[row]
        source_h = self._wrapped_height(
            str(getattr(seg, "text", "") or ""), self._table.columnWidth(3)
        )
        target_h = self._wrapped_height(
            str(seg.target_text or ""), self._table.columnWidth(4)
        )
        editor_h = target_h + _EDITOR_CHROME
        height = max(_MIN_ROW_HEIGHT, source_h + _CELL_V_PAD, editor_h + _CELL_V_PAD)
        self._table.setRowHeight(row, height)
        self._editors[row].setFixedHeight(min(editor_h, height - _CELL_V_PAD))

    def _update_all_row_heights(self) -> None:
        """按当前列宽重算全部行高（列宽变化/首次布局后调用）。"""
        for row in range(len(self._segments)):
            self._update_row_height(row)

    # -------------------------------------------------------------- 连续试听

    def _on_continuous_clicked(self) -> None:
        """连续试听：从当前选中句播放到最后一句；播放中再次点击则停止。"""
        if self._continuous_playing:
            self._stop_continuous()
            self._player.stop()
            return
        start = self._table.currentRow()
        if start < 0 or start >= len(self._segments):
            start = 0  # 无当前项时从第一句开始
        self._continuous_playing = True
        self._continuous_button.setText(tr("player.stop"))
        self._continuous_slider.setEnabled(True)
        self._advance_continuous(start)

    def _on_continuous_slider_released(self) -> None:
        """拖动连续试听滑块松开：跳到对应句继续播放。"""
        if self._continuous_playing:
            self._advance_continuous(self._continuous_slider.value())

    def _advance_continuous(self, index: int) -> None:
        """连续试听推进到第 index 句；文件缺失时顺延下一句，直至播完。"""
        if not self._continuous_playing:
            return
        while 0 <= index < len(self._segments):
            seg = self._segments[index]
            path = str(getattr(seg, "tts_audio_path", "") or "")
            if path and os.path.isfile(path):
                self._continuous_index = index
                self._continuous_slider.setValue(index)
                self._continuous_count_label.setText(
                    f"{index + 1} / {len(self._segments)}"
                )
                self._player.set_source(path)
                self._player.play()
                return
            index += 1
        self._stop_continuous()

    def _stop_continuous(self) -> None:
        """退出连续试听状态，恢复按钮文字与进度显示。"""
        if not self._continuous_playing:
            return
        self._continuous_playing = False
        self._continuous_index = -1
        self._continuous_button.setText(tr("tts_review.continuous_listen"))
        self._continuous_slider.setValue(0)
        self._continuous_slider.setEnabled(False)
        self._continuous_count_label.setText(f"0 / {len(self._segments)}")

    def _on_player_finished(self) -> None:
        """一句播放自然结束：连续试听中自动接播下一句。"""
        if self._continuous_playing:
            self._advance_continuous(self._continuous_index + 1)

    # -------------------------------------------------------------- 重生成队列

    def _enqueue_regen(self, rows: list[int]) -> None:
        """把行加入重生成队列（跳过已排队/生成中的行），按需启动后台线程。"""
        added: list[int] = []
        with self._regen_lock:
            for r in rows:
                if not (0 <= r < len(self._regen_buttons)):
                    continue
                if r in self._regen_states or r in self._regen_pending:
                    continue
                self._regen_pending.append(r)
                added.append(r)
            start = added and not self._regen_active
            if start:
                self._regen_active = True
                self._regen_stop = False
                self._regen_thread = threading.Thread(
                    target=self._regen_worker, daemon=True,
                )
        if not added:
            return
        for r in added:
            self._regen_states[r] = "waiting"
            self._regen_buttons[r].setText(tr("tts_review.regen_waiting"))
            self._regen_buttons[r].setEnabled(False)
        self._ok_button.setEnabled(False)  # 生成期间不能进入下一步
        self._refresh_stats()
        if start:
            self._regen_thread.start()

    def _regen_worker(self) -> None:
        """后台线程：逐句调用 regen_hook 重生成（runner 线程此时阻塞在
        等待审核结果，不会并发使用 TTS 引擎）。"""
        while True:
            with self._regen_lock:
                if self._regen_stop or not self._regen_pending:
                    break
                row = self._regen_pending.popleft()
            self._regen_started.emit(row)
            ratio = None
            error = ""
            try:
                if self._regen_hook is None:
                    error = tr("tts_review.regen_unavailable")
                else:
                    # progress：经信号转回主线程，按钮上显示生成百分比
                    ratio = self._regen_hook(
                        row,
                        progress=lambda frac, desc="": self._regen_progress.emit(
                            row, float(frac),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 —— 单句失败不中断队列
                error = str(exc) or exc.__class__.__name__
            self._regen_finished.emit(row, ratio, error)
        self._regen_queue_done.emit()

    def _on_regen_started(self, row: int) -> None:
        """主线程：该句开始生成——释放播放器句柄（同名 wav 将被覆盖）。"""
        self._stop_continuous()  # 连续试听中先退出，避免播放被覆盖的文件
        self._player.release()
        self._regen_states[row] = "running"
        self._regen_buttons[row].setText(tr("tts_review.regen_running"))
        self._refresh_stats()

    def _on_regen_progress(self, row: int, frac: float) -> None:
        """主线程：生成中的句子按钮显示百分比进度（锚点+插值混合推进）。"""
        if self._regen_states.get(row) == "running":
            self._regen_buttons[row].setText(
                tr("tts_review.regen_progress", percent=int(round(frac * 100)))
            )

    def _on_regen_finished(self, row: int, ratio, error: str) -> None:
        """主线程：该句生成完成——恢复按钮并刷新伸缩比/试听/统计。"""
        self._regen_states.pop(row, None)
        self._regen_buttons[row].setText(tr("tts_review.regenerate_this"))
        self._regen_buttons[row].setEnabled(True)
        seg = self._segments[row]
        if not error:
            self._table.setItem(row, 5, _ratio_item(seg))
            path = str(getattr(seg, "tts_audio_path", "") or "")
            self._listen_buttons[row].setEnabled(
                bool(path) and os.path.isfile(path)
            )
        else:
            QMessageBox.warning(
                self, tr("tts_review.regen_failed_title"),
                tr("tts_review.regen_failed", index=row + 1, error=error),
            )
        self._refresh_stats()

    def _on_regen_queue_done(self) -> None:
        """主线程：队列排空——恢复界面；被中途追加的行重新开线程处理。"""
        with self._regen_lock:
            restart = bool(self._regen_pending) and not self._regen_stop
            if restart:
                self._regen_thread = threading.Thread(
                    target=self._regen_worker, daemon=True,
                )
        if restart:
            self._regen_thread.start()
            return
        self._regen_active = False
        self._regen_states.clear()
        for button in self._regen_buttons:
            button.setText(tr("tts_review.regenerate_this"))
            button.setEnabled(True)
        self._ok_button.setEnabled(True)
        self._cancel_button.setEnabled(True)
        self._refresh_stats()
        if self._regen_stop:
            self._regen_stop = False
            super().reject()
