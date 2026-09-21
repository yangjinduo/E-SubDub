# -*- coding: utf-8 -*-
"""ASR 识别结果人工校对对话框。

逐句核对：修改说话人与文本、标记删除/恢复，并按句切取人声片段试听。
Segment 采用鸭子类型：仅访问 spk/start/end/text/deleted 等属性，
与 app.core.segment.Segment 完全兼容。
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.ffmpeg import get_ffmpeg_path
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

_COLOR_NORMAL = QColor("#1F2937")
_COLOR_DELETED = QColor("#9CA3AF")
_COLOR_BG_DELETED = QColor("#F3F4F6")
_COLOR_BG_NORMAL = QColor("#FFFFFF")


def _fmt_time(seconds: float) -> str:
    """秒 → "分:秒.十分位"（如 83.46 → "01:23.4"）。"""
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


def _extract_clip(audio_wav: str, start: float, end: float) -> "str | None":
    """用 ffmpeg 从整条音轨切取 [start, end) 片段到临时 wav，失败返回 None。"""
    if not audio_wav or not os.path.isfile(audio_wav):
        return None
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return None
    duration = max(end - start, 0.05)
    fd, tmp_path = tempfile.mkstemp(prefix="subdub_clip_", suffix=".wav")
    os.close(fd)
    cmd = [
        ffmpeg,
        "-ss", f"{start:.3f}",
        "-i", audio_wav,
        "-t", f"{duration:.3f}",
        "-y", tmp_path,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode == 0 and os.path.isfile(tmp_path) and os.path.getsize(tmp_path) > 44:
        return tmp_path
    try:
        os.remove(tmp_path)
    except OSError:
        pass
    return None


class AsrReviewDialog(QDialog):
    """ASR 结果校对对话框。

    编辑即时回写传入的 Segment 对象属性，确认后 get_result() 返回同一列表。
    """

    def __init__(self, segments: list, audio_wav: str, parent=None) -> None:
        super().__init__(parent)
        self._segments = segments  # 直接持有原列表，就地修改
        self._audio_wav = audio_wav or ""
        self._clip_cache: "dict[tuple[float, float], str]" = {}
        self._action_buttons: list[QPushButton] = []

        self.setWindowTitle(tr("asr_review.title"))
        self.resize(900, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题 + 统计
        title_row = QHBoxLayout()
        title = QLabel(tr("asr_review.heading"))
        title.setObjectName("sectionTitle")
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #6B7280;")
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self._stats_label)
        layout.addLayout(title_row)

        # 主表格
        self._table = QTableWidget(len(self._segments), 7, self)
        self._table.setHorizontalHeaderLabels(
            [
                tr("asr_review.column_index"),
                tr("asr_review.column_start"),
                tr("asr_review.column_end"),
                tr("asr_review.column_speaker"),
                tr("asr_review.column_text"),
                tr("asr_review.column_status"),
                tr("asr_review.column_actions"),
            ]
        )
        self._table.setStyleSheet(TABLE_QSS)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(42)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 52), (1, 84), (2, 84), (3, 96), (5, 100), (6, 84)):
            self._table.setColumnWidth(col, width)
        layout.addWidget(self._table, stretch=1)

        # 底部试听条
        self._player = AudioPlayerBar(self)
        layout.addWidget(self._player)

        # 按钮行
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        cancel_button = QPushButton(tr("common.cancel"))
        cancel_button.clicked.connect(self.reject)
        ok_button = QPushButton(tr("common.confirm"))
        ok_button.setObjectName("primaryButton")
        ok_button.clicked.connect(self.accept)
        button_row.addWidget(cancel_button)
        button_row.addWidget(ok_button)
        layout.addLayout(button_row)

        self._fill_rows()
        self._refresh_stats()

    # -------------------------------------------------------------- 公开接口

    def get_result(self) -> list:
        """返回修改后的分段列表（即传入的原列表，属性已就地更新）。"""
        return self._segments

    # -------------------------------------------------------------- 内部构建

    def _fill_rows(self) -> None:
        """填充表格行：说话人下拉选项 = 全部出现过的 spk 并排序。"""
        speakers = sorted({str(seg.spk) for seg in self._segments})
        center = Qt.AlignmentFlag.AlignCenter
        for row, seg in enumerate(self._segments):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, 0, index_item)
            self._table.setItem(row, 1, QTableWidgetItem(_fmt_time(float(seg.start))))
            self._table.setItem(row, 2, QTableWidgetItem(_fmt_time(float(seg.end))))

            combo = QComboBox()
            combo.setStyleSheet("QComboBox { min-width: 0; padding: 4px 8px; }")
            combo.addItems(speakers)
            combo.setCurrentText(str(seg.spk))
            combo.currentTextChanged.connect(
                lambda text, r=row: self._on_spk_changed(r, text)
            )
            self._table.setCellWidget(row, 3, combo)

            edit = QLineEdit(str(seg.text))
            edit.setStyleSheet("QLineEdit { padding: 4px 8px; }")
            edit.textChanged.connect(lambda text, r=row: self._on_text_changed(r, text))
            self._table.setCellWidget(row, 4, edit)

            status_item = QTableWidgetItem(tr("asr_review.status_normal"))
            status_item.setTextAlignment(center)
            self._table.setItem(row, 5, status_item)

            action = QPushButton(tr("asr_review.mark_delete"))
            action.setObjectName("cardSettingButton")
            action.clicked.connect(lambda _=False, r=row: self._toggle_delete_state(r))
            self._action_buttons.append(action)
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, 2, 6, 2)
            holder_layout.addWidget(action)
            self._table.setCellWidget(row, 6, holder)

            self._apply_row_state(row)

    def _apply_row_state(self, row: int) -> None:
        """按 deleted 状态刷新整行外观：置灰 / 恢复、状态文本、操作按钮。"""
        seg = self._segments[row]
        deleted = bool(seg.deleted)
        for col in (0, 1, 2):
            item = self._table.item(row, col)
            item.setForeground(_COLOR_DELETED if deleted else _COLOR_NORMAL)
            item.setBackground(_COLOR_BG_DELETED if deleted else _COLOR_BG_NORMAL)

        status_item = self._table.item(row, 5)
        status_item.setText(
            tr("asr_review.status_deleted") if deleted else tr("asr_review.status_normal")
        )
        status_item.setForeground(QColor("#DC2626") if deleted else QColor("#059669"))

        self._table.cellWidget(row, 3).setEnabled(not deleted)
        self._table.cellWidget(row, 4).setEnabled(not deleted)
        self._action_buttons[row].setText(
            tr("asr_review.restore") if deleted else tr("asr_review.mark_delete")
        )

    def _refresh_stats(self) -> None:
        """刷新顶部统计标签。"""
        total = len(self._segments)
        deleted = sum(1 for seg in self._segments if seg.deleted)
        speakers = {str(seg.spk) for seg in self._segments}
        self._stats_label.setText(
            tr("asr_review.stats", total=total, deleted=deleted, speakers=len(speakers))
        )

    # -------------------------------------------------------------- 内部槽

    def _on_spk_changed(self, row: int, text: str) -> None:
        self._segments[row].spk = text

    def _on_text_changed(self, row: int, text: str) -> None:
        self._segments[row].text = text

    def _toggle_delete_state(self, row: int) -> None:
        """切换某行的删除标记（“删除”/“恢复”按钮共用槽）。"""
        seg = self._segments[row]
        seg.deleted = not bool(seg.deleted)
        self._apply_row_state(row)
        self._refresh_stats()

    def _on_selection_changed(self) -> None:
        """选中行后：切取该句人声片段并在底部试听。"""
        if not self._audio_wav:
            return
        row = self._table.currentRow()
        if row < 0 or row >= len(self._segments):
            return
        seg = self._segments[row]
        clip = self._get_clip(seg)
        if clip:
            self._player.set_source(clip)
            self._player.play()
        else:
            self._player.set_source("")

    # -------------------------------------------------------------- 工具

    def _get_clip(self, seg) -> str:
        """取该句片段的临时 wav 路径（按起止时间缓存），失败返回空串。"""
        start, end = float(seg.start), float(seg.end)
        key = (round(start, 3), round(end, 3))
        if key not in self._clip_cache:
            self._clip_cache[key] = _extract_clip(self._audio_wav, start, end) or ""
        return self._clip_cache[key]
