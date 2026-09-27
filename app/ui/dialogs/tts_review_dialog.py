# -*- coding: utf-8 -*-
"""TTS 配音结果复核对话框。

显示全部配音句子：试听生成的配音、修改译文、即时重生成不满意的句子
（点击"重新生成此句"或批量按钮后由 regen_hook 在后台逐句排队执行，
生成中的句子显示"生成中…"、其余排队句显示"等待重生成"，完成后可
再次试听或重生成）；伸缩比异常（needs_review）的句子数值以红色粗体
标出。Segment 采用鸭子类型，与 app.core.segment.Segment 完全兼容。
"""

from __future__ import annotations

import os
import threading
from collections import deque

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
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
        item.setForeground(QColor("#DC2626"))
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
        self.resize(920, 660)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题
        title = QLabel(tr("tts_review.heading"))
        title.setObjectName("sectionTitle")
        layout.addWidget(title)

        # 主表格
        self._table = QTableWidget(len(self._segments), 7, self)
        self._table.setHorizontalHeaderLabels(
            [
                tr("tts_review.column_index"),
                tr("tts_review.column_time"),
                tr("tts_review.column_speaker"),
                tr("tts_review.column_target"),
                tr("tts_review.column_ratio"),
                tr("tts_review.column_listen"),
                tr("tts_review.column_regen"),
            ]
        )
        self._table.setStyleSheet(TABLE_QSS)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(42)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 46), (1, 130), (2, 70), (4, 70), (5, 70), (6, 110)):
            self._table.setColumnWidth(col, width)
        layout.addWidget(self._table, stretch=1)

        # 底部试听条
        self._player = AudioPlayerBar(self)
        layout.addWidget(self._player)

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

    # -------------------------------------------------------------- 公开接口

    def get_regenerate_indices(self) -> "list[int]":
        """兼容旧接口：重生成已改为对话框内即时完成，恒返回空列表。"""
        return []

    def get_edited_segments(self) -> list:
        """返回包含译文修改的分段列表（即传入的原列表，已就地更新）。"""
        return self._segments

    def done(self, result: int) -> None:
        """关闭前释放播放器持有的配音文件句柄（重生成会覆盖同名 wav）。"""
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

            edit = QLineEdit(str(seg.target_text))
            edit.setStyleSheet("QLineEdit { padding: 4px 8px; }")
            edit.textChanged.connect(
                lambda text, r=row: self._on_target_changed(r, text)
            )
            self._table.setCellWidget(row, 3, edit)

            self._table.setItem(row, 4, _ratio_item(seg))

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
            self._table.setCellWidget(row, 5, listen_holder)

            # 重新生成按钮：点击后即时排队重生成（生成期间显示状态）
            regen_button = QPushButton(tr("tts_review.regenerate_this"))
            regen_button.setObjectName("cardSettingButton")
            regen_button.clicked.connect(
                lambda _=False, r=row: self._enqueue_regen([r])
            )
            self._regen_buttons.append(regen_button)
            self._table.setCellWidget(row, 6, regen_button)

    def _refresh_stats(self) -> None:
        """刷新底部统计标签（总数 / 异常数 / 待重生成数）。"""
        total = len(self._segments)
        abnormal = sum(1 for seg in self._segments if getattr(seg, "needs_review", False))
        pending = len(self._regen_states)
        self._stats_label.setText(
            tr("tts_review.stats", total=total, abnormal=abnormal, pending=pending)
        )

    # -------------------------------------------------------------- 内部槽

    def _on_target_changed(self, row: int, text: str) -> None:
        """译文编辑：即时回写 Segment。"""
        self._segments[row].target_text = text

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
        重生成后新文件名也能正确链接）。"""
        seg = self._segments[row]
        path = str(getattr(seg, "tts_audio_path", "") or "")
        if path and os.path.isfile(path):
            self._player.set_source(path)
            self._player.play()

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
            self._table.setItem(row, 4, _ratio_item(seg))
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
