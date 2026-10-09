# -*- coding: utf-8 -*-
"""文本转语音复核对话框。

显示逐句合成结果：试听、删除（不进成品，WAV 保留可恢复）、即时重生成
（点击"重新生成"后由 regen_hook 在后台逐句排队执行，生成中的句子显示
百分比、排队句显示"等待重生成"，完成后可再次试听或重生成）。时间列
显示成品时间轴（保留句实际时长顺序累计，删除句不占位）；情绪列点击
可直接更换该句情绪并自动入队重生成（排队中的句子更新后按新情绪生成）；
连续试听从当前句一路播放到最后一句（自动跳过已删除句）。rows 为
dict 列表（text/emo/file/duration/deleted），由 runner 构造并就地更新。

对话框关闭与重生成前必须释放播放器文件句柄（同名 WAV 将被覆盖）。
"""

from __future__ import annotations

import os
import threading
from collections import deque

from PySide6.QtCore import QRect, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
from app.core.tts_runner import INTERVAL_SILENCE_SEC
from app.ui.dialogs.tts_edit_dialog import EMOTION_ITEMS, emotion_color, emotion_label
from app.ui.widgets.audio_player import AudioPlayerBar

# 表格样式：与 app/ui/style.py 全局 QSS 保持一致的浅色现代观感
TABLE_QSS = """
QTableWidget {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
    gridline-color: #F3F4F6;
    selection-background-color: #EFF6FF;
    selection-color: #1D4ED8;
}
QHeaderView::section {
    background: #F9FAFB;
    color: #4B5563;
    border: none;
    border-bottom: 1px solid #E5E7EB;
    padding: 8px 6px;
    font-weight: 600;
}
"""

# 行高自适应：最小行高与内容换行上下的留白
_MIN_ROW_HEIGHT = 42
_CELL_V_PAD = 8

# 列号
_COL_INDEX, _COL_TIME, _COL_CONTENT = 0, 1, 2
_COL_EMO, _COL_LISTEN, _COL_DELETE, _COL_REGEN = 3, 4, 5, 6

# 情绪菜单取消哨兵（"跟随参考音频"的 key 本身就是 None，须与之区分）
_MENU_CANCELLED = object()


def _fmt_time(seconds: float) -> str:
    """秒 → "分:秒.十分位"（如 83.46 → "01:23.4"）。"""
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


def _dot_icon(color: str) -> QIcon:
    """情绪菜单项的色点图标（与编辑对话框情绪颜色对应）。"""
    pixmap = QPixmap(14, 14)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.drawEllipse(3, 3, 8, 8)
    painter.end()
    return QIcon(pixmap)


class TtsVoiceReviewDialog(QDialog):
    """文本转语音语音复核对话框。

    rows 就地更新（删除标记 / 重生成后的时长），确认后 runner 直接以
    当前行集保存复核结果；重生成在对话框内即时完成（regen_hook 后台
    逐句执行，runner 线程此时阻塞在等待复核结果，不会并发使用引擎）。
    """

    _regen_started = Signal(int)                # 句子开始生成（行号）
    _regen_finished = Signal(int, object, str)  # 句子生成完成（行号, 时长|None, 错误）
    _regen_progress = Signal(int, float)        # 句子生成进度（行号, 0~1）
    _regen_queue_done = Signal()                # 队列排空（或被取消）

    def __init__(
        self, rows: list, ws_dir: str = "", parent=None, regen_hook=None,
    ) -> None:
        super().__init__(parent)
        self._rows = rows          # 全部句子行（dict），就地更新
        self._ws_dir = str(ws_dir or "")
        self._regen_hook = regen_hook  # 即时重生成回调：行号 → 新时长
        self._listen_buttons: list[QPushButton] = []
        self._delete_buttons: list[QPushButton] = []
        self._regen_buttons: list[QPushButton] = []
        self._content_items: list[QTableWidgetItem] = []
        self._continuous_playing = False  # 连续试听进行中
        self._continuous_index = -1       # 连续试听当前播放行

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

        self.setWindowTitle(tr("tts_voice_review.title"))
        self.resize(980, 660)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题 + 提示
        title = QLabel(tr("tts_voice_review.heading"))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        tip = QLabel(tr("tts_voice_review.tip"))
        tip.setWordWrap(True)
        tip.setStyleSheet("color: #6B7280;")
        layout.addWidget(tip)

        # 主表格
        self._table = QTableWidget(len(self._rows), 7, self)
        self._table.setHorizontalHeaderLabels([
            tr("tts_voice_review.column_index"),
            tr("tts_voice_review.column_time"),
            tr("tts_voice_review.column_content"),
            tr("tts_voice_review.column_emotion"),
            tr("tts_voice_review.column_listen"),
            tr("tts_voice_review.column_delete"),
            tr("tts_voice_review.column_regen"),
        ])
        self._table.setStyleSheet(TABLE_QSS)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_MIN_ROW_HEIGHT)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(_COL_CONTENT, QHeaderView.ResizeMode.Stretch)
        for col, width in (
            (_COL_INDEX, 46), (_COL_TIME, 130), (_COL_EMO, 90),
            (_COL_LISTEN, 70), (_COL_DELETE, 70), (_COL_REGEN, 110),
        ):
            self._table.setColumnWidth(col, width)
        # 情绪列点击 → 弹菜单更换该句情绪（更换后自动入队重生成）
        self._table.cellClicked.connect(self._on_cell_clicked)
        layout.addWidget(self._table, stretch=1)

        # 列宽变化后重算各行换行行高；拖拽过程中用单发定时器合并
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(40)
        self._height_timer.timeout.connect(self._update_all_row_heights)
        header.sectionResized.connect(lambda *_: self._height_timer.start())

        # 底部试听条
        self._player = AudioPlayerBar(self)
        self._player.finished.connect(self._on_player_finished)
        layout.addWidget(self._player)

        # 连续试听行
        continuous_row = QHBoxLayout()
        continuous_row.setContentsMargins(0, 0, 0, 0)
        continuous_row.setSpacing(10)
        self._continuous_button = QPushButton(tr("tts_voice_review.continuous_listen"))
        self._continuous_button.setFixedWidth(72)
        self._continuous_button.setStyleSheet("QPushButton { padding: 6px 8px; }")
        self._continuous_button.clicked.connect(self._on_continuous_clicked)
        self._continuous_slider = QSlider(Qt.Orientation.Horizontal)
        self._continuous_slider.setRange(0, max(len(self._rows) - 1, 0))
        self._continuous_slider.setValue(0)
        self._continuous_slider.setEnabled(False)
        self._continuous_slider.sliderReleased.connect(self._on_continuous_slider_released)
        self._continuous_count_label = QLabel(f"0 / {len(self._rows)}")
        self._continuous_count_label.setStyleSheet("color: #6B7280; font-size: 12px;")
        self._continuous_count_label.setMinimumWidth(110)
        continuous_row.addWidget(self._continuous_button)
        continuous_row.addWidget(self._continuous_slider, stretch=1)
        continuous_row.addWidget(self._continuous_count_label)
        layout.addLayout(continuous_row)

        # 统计行
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #6B7280;")
        layout.addWidget(self._stats_label)

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
        self._refresh_times()
        self._refresh_stats()
        QTimer.singleShot(0, self._update_all_row_heights)

    # -------------------------------------------------------------- 公开接口

    def done(self, result: int) -> None:
        """关闭前释放播放器持有的音频文件句柄（重生成会覆盖同名 WAV）。"""
        self._continuous_playing = False
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

    # -------------------------------------------------------------- 内部构建

    def _row_path(self, row: int) -> str:
        """该行音频文件的绝对路径（file 为工作夹相对路径）。"""
        rel = str(self._rows[row].get("file") or "")
        if rel and self._ws_dir:
            return os.path.join(self._ws_dir, rel)
        return rel

    def _fill_rows(self) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        for row in range(len(self._rows)):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, _COL_INDEX, index_item)
            # 时间列在 _refresh_times 中填充（随删除/重生成变化）
            self._table.setItem(row, _COL_TIME, QTableWidgetItem(""))

            content = QTableWidgetItem(str(self._rows[row].get("text") or ""))
            content.setToolTip(str(self._rows[row].get("text") or ""))
            self._table.setItem(row, _COL_CONTENT, content)
            self._content_items.append(content)

            self._set_emotion_cell(row)

            listen_button = QPushButton(tr("tts_voice_review.listen"))
            listen_button.setObjectName("cardSettingButton")
            path = self._row_path(row)
            listen_button.setEnabled(bool(path) and os.path.isfile(path))
            listen_button.clicked.connect(
                lambda _=False, r=row: self._on_listen_clicked(r)
            )
            self._listen_buttons.append(listen_button)
            listen_holder = QWidget()
            listen_layout = QHBoxLayout(listen_holder)
            listen_layout.setContentsMargins(6, 2, 6, 2)
            listen_layout.addWidget(listen_button)
            self._table.setCellWidget(row, _COL_LISTEN, listen_holder)

            delete_button = QPushButton(tr("common.delete"))
            delete_button.setObjectName("cardSettingButton")
            delete_button.clicked.connect(
                lambda _=False, r=row: self._on_delete_clicked(r)
            )
            self._delete_buttons.append(delete_button)
            self._table.setCellWidget(row, _COL_DELETE, delete_button)

            regen_button = QPushButton(tr("tts_voice_review.regenerate_this"))
            regen_button.setObjectName("cardSettingButton")
            regen_button.clicked.connect(
                lambda _=False, r=row: self._on_regen_clicked(r)
            )
            self._regen_buttons.append(regen_button)
            self._table.setCellWidget(row, _COL_REGEN, regen_button)

            self._apply_row_state(row)

    # -------------------------------------------------------------- 行状态/时间轴

    def _apply_row_state(self, row: int) -> None:
        """按删除标记与重生成队列状态刷新行内控件。"""
        deleted = bool(self._rows[row].get("deleted"))
        self._delete_buttons[row].setText(
            tr("tts_voice_review.restore") if deleted else tr("common.delete")
        )
        color = QColor("#9CA3AF") if deleted else QColor("#1F2937")
        self._content_items[row].setForeground(color)
        self._table.item(row, _COL_EMO).setForeground(
            QColor("#9CA3AF") if deleted
            else QColor(emotion_color(self._rows[row].get("emo")))
        )
        in_queue = row in self._regen_states
        self._regen_buttons[row].setEnabled(not deleted and not in_queue)

    def _set_emotion_cell(self, row: int) -> None:
        """按 rows[row]["emo"] 刷新情绪单元格（文字 / 颜色 / 提示）。"""
        emo_key = self._rows[row].get("emo")
        emo_item = QTableWidgetItem(emotion_label(emo_key))
        emo_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
        emo_item.setForeground(QColor(emotion_color(emo_key)))
        emo_item.setToolTip(tr("tts_voice_review.emotion_tooltip"))
        self._table.setItem(row, _COL_EMO, emo_item)

    def _on_cell_clicked(self, row: int, col: int) -> None:
        """情绪列点击 → 弹菜单更换该句情绪并自动入队重生成。

        已删除句不进成品（改情绪无意义）；生成中的句子本次用的还是旧
        情绪，改了易误解，两者忽略。等待重生成的句子允许改：入队时只
        记行号，_regen_one 执行时读取最新 emo，新情绪直接生效。
        """
        if col != _COL_EMO:
            return
        if self._rows[row].get("deleted"):
            return
        if self._regen_states.get(row) == "running":
            return
        key = self._pick_emotion(row)
        if key is _MENU_CANCELLED or key == self._rows[row].get("emo"):
            return
        self._rows[row]["emo"] = key
        self._set_emotion_cell(row)
        if bool(self._rows[row].get("deleted")):
            return
        if row not in self._regen_states:
            self._enqueue_regen([row])

    def _pick_emotion(self, row: int):
        """在情绪单元格下方弹出情绪菜单，返回所选 key；取消返回
        _MENU_CANCELLED；key=None 表示"跟随参考音频"。"""
        current = self._rows[row].get("emo")
        menu = QMenu(self)
        for key, i18n_key, color in EMOTION_ITEMS:
            action = menu.addAction(_dot_icon(color), tr(i18n_key))
            action.setData(key if key is not None else "")
            if key == current:
                action.setCheckable(True)
                action.setChecked(True)
        item = self._table.item(row, _COL_EMO)
        if item is None:
            pos = None
        else:
            rect = self._table.visualItemRect(item)
            pos = self._table.viewport().mapToGlobal(rect.bottomLeft())
        chosen = menu.exec(pos)
        if chosen is None:
            return _MENU_CANCELLED
        data = chosen.data()
        return data if data != "" else None

    def _timeline(self) -> "list[tuple[float, float] | None]":
        """成品时间轴：保留句实际时长顺序累计（删除句不占位，记 None）。"""
        spans: "list[tuple[float, float] | None]" = []
        cursor = 0.0
        for row in self._rows:
            if row.get("deleted"):
                spans.append(None)
                continue
            duration = float(row.get("duration") or 0.0)
            spans.append((cursor, cursor + duration))
            cursor += duration + INTERVAL_SILENCE_SEC
        return spans

    def _refresh_times(self) -> None:
        """按当前删除标记/时长刷新时间列（删除句显示 "-"）。"""
        for row, span in enumerate(self._timeline()):
            text = (
                f"{_fmt_time(span[0])} - {_fmt_time(span[1])}"
                if span else "-"
            )
            self._table.setItem(
                row, _COL_TIME, QTableWidgetItem(text)
            )

    def _refresh_stats(self) -> None:
        """刷新底部统计标签（总数 / 已删除数 / 待重生成数）。"""
        total = len(self._rows)
        deleted = sum(1 for row in self._rows if row.get("deleted"))
        pending = len(self._regen_states)
        self._stats_label.setText(
            tr("tts_voice_review.stats",
               total=total, deleted=deleted, pending=pending)
        )

    # -------------------------------------------------------------- 内部槽

    def _on_delete_clicked(self, row: int) -> None:
        """删除/恢复该句：删除句不进成品（WAV 保留），可再点恢复。"""
        self._rows[row]["deleted"] = not bool(self._rows[row].get("deleted"))
        self._apply_row_state(row)
        self._refresh_times()
        self._refresh_stats()

    def _on_listen_clicked(self, row: int) -> None:
        """试听该句合成结果（删除句也可试听，便于决定是否恢复）。"""
        self._stop_continuous()
        path = self._row_path(row)
        if path and os.path.isfile(path):
            self._player.set_source(path)
            self._player.play()

    def _on_regen_clicked(self, row: int) -> None:
        """单句重生成：排队后保持当前选中行与焦点不变。"""
        current = self._table.currentRow()
        self._enqueue_regen([row])
        self._table.setFocus()
        if current >= 0 and self._table.currentRow() != current:
            self._table.setCurrentCell(current, max(self._table.currentColumn(), 0))

    # -------------------------------------------------------------- 行高自适应

    def _wrapped_height(self, text: str, width: int) -> int:
        """估算文本按给定宽度自动换行后所需的高度（像素）。"""
        fm = QFontMetrics(self._table.font())
        avail = max(width - 16, 30)
        rect = fm.boundingRect(QRect(0, 0, avail, 100000), Qt.TextFlag.TextWordWrap, text)
        return rect.height()

    def _update_all_row_heights(self) -> None:
        """按当前列宽重算全部行高（列宽变化/首次布局后调用）。"""
        width = self._table.columnWidth(_COL_CONTENT)
        for row in range(len(self._rows)):
            text = str(self._rows[row].get("text") or "")
            height = max(_MIN_ROW_HEIGHT,
                         self._wrapped_height(text, width) + _CELL_V_PAD)
            self._table.setRowHeight(row, height)

    # -------------------------------------------------------------- 连续试听

    def _on_continuous_clicked(self) -> None:
        """连续试听：从当前选中句播放到最后一句（跳过已删除句）；
        播放中再次点击则停止。"""
        if self._continuous_playing:
            self._stop_continuous()
            self._player.stop()
            return
        start = self._table.currentRow()
        if start < 0 or start >= len(self._rows):
            start = 0
        self._continuous_playing = True
        self._continuous_button.setText(tr("player.stop"))
        self._continuous_slider.setEnabled(True)
        self._advance_continuous(start)

    def _on_continuous_slider_released(self) -> None:
        """拖动连续试听滑块松开：跳到对应句继续播放。"""
        if self._continuous_playing:
            self._advance_continuous(self._continuous_slider.value())

    def _advance_continuous(self, index: int) -> None:
        """连续试听推进到第 index 句；已删除句与缺失文件顺延，直至播完。"""
        if not self._continuous_playing:
            return
        while 0 <= index < len(self._rows):
            if not self._rows[index].get("deleted"):
                path = self._row_path(index)
                if path and os.path.isfile(path):
                    self._continuous_index = index
                    self._continuous_slider.setValue(index)
                    self._continuous_count_label.setText(
                        f"{index + 1} / {len(self._rows)}"
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
        self._continuous_button.setText(tr("tts_voice_review.continuous_listen"))
        self._continuous_slider.setValue(0)
        self._continuous_slider.setEnabled(False)
        self._continuous_count_label.setText(f"0 / {len(self._rows)}")

    def _on_player_finished(self) -> None:
        """一句播放自然结束：连续试听中自动接播下一句。"""
        if self._continuous_playing:
            self._advance_continuous(self._continuous_index + 1)

    # -------------------------------------------------------------- 重生成队列

    def _enqueue_regen(self, rows: list[int]) -> None:
        """把行加入重生成队列（跳过已排队/生成中/已删除的行），按需启动后台线程。"""
        added: list[int] = []
        with self._regen_lock:
            for r in rows:
                if not (0 <= r < len(self._regen_buttons)):
                    continue
                if r in self._regen_states or r in self._regen_pending:
                    continue
                if self._rows[r].get("deleted"):
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
            self._regen_buttons[r].setText(tr("tts_voice_review.regen_waiting"))
        self._ok_button.setEnabled(False)  # 生成期间不能进入下一步
        self._refresh_stats()
        if start:
            self._regen_thread.start()

    def _regen_worker(self) -> None:
        """后台线程：逐句调用 regen_hook 重生成（runner 线程此时阻塞在
        等待复核结果，不会并发使用 TTS 引擎）。"""
        while True:
            with self._regen_lock:
                if self._regen_stop or not self._regen_pending:
                    break
                row = self._regen_pending.popleft()
            self._regen_started.emit(row)
            duration = None
            error = ""
            try:
                if self._regen_hook is None:
                    error = tr("tts_voice_review.regen_unavailable")
                else:
                    # progress：经信号转回主线程，按钮上显示生成百分比
                    duration = self._regen_hook(
                        row,
                        progress=lambda frac, desc="": self._regen_progress.emit(
                            row, float(frac),
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 —— 单句失败不中断队列
                error = str(exc) or exc.__class__.__name__
            self._regen_finished.emit(row, duration, error)
        self._regen_queue_done.emit()

    def _on_regen_started(self, row: int) -> None:
        """主线程：该句开始生成——释放播放器句柄（同名 WAV 将被覆盖）。"""
        self._stop_continuous()
        self._player.release()
        self._regen_states[row] = "running"
        self._regen_buttons[row].setText(tr("tts_voice_review.regen_running"))
        self._refresh_stats()

    def _on_regen_progress(self, row: int, frac: float) -> None:
        """主线程：生成中的句子按钮显示百分比进度。"""
        if self._regen_states.get(row) == "running":
            self._regen_buttons[row].setText(
                tr("tts_voice_review.regen_progress",
                   percent=int(round(frac * 100)))
            )

    def _on_regen_finished(self, row: int, duration, error: str) -> None:
        """主线程：该句生成完成——恢复按钮并刷新时间轴/试听/统计。"""
        self._regen_states.pop(row, None)
        self._regen_buttons[row].setText(tr("tts_voice_review.regenerate_this"))
        self._apply_row_state(row)
        if not error:
            self._refresh_times()  # 新时长改变成品时间轴
            path = self._row_path(row)
            self._listen_buttons[row].setEnabled(
                bool(path) and os.path.isfile(path)
            )
        else:
            QMessageBox.warning(
                self, tr("tts_voice_review.regen_failed_title"),
                tr("tts_voice_review.regen_failed",
                   index=row + 1, error=error),
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
        for row in range(len(self._regen_buttons)):
            self._apply_row_state(row)
            self._regen_buttons[row].setText(tr("tts_voice_review.regenerate_this"))
        self._ok_button.setEnabled(True)
        self._cancel_button.setEnabled(True)
        self._refresh_stats()
        if self._regen_stop:
            self._regen_stop = False
            super().reject()
