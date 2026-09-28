# -*- coding: utf-8 -*-
"""共用分段表格控件：表格 + 试听条组合，选中行即切取片段播放。

从 ASR 校对对话框抽取，供编辑类对话框与各流程结果查看器复用：
- 编辑模式（readonly=False）：说话人下拉 + 文本编辑框（自动换行、
  行高随内容自适应）+ 删除/恢复按钮
- 只读模式（readonly=True）：纯文本展示，仍支持选中行试听
Segment 采用鸭子类型：仅访问 spk/start/end/text/deleted 等属性，
与 app.core.segment.Segment 完全兼容。
"""

from __future__ import annotations

import os
import subprocess
import tempfile

from PySide6.QtCore import QEvent, QRect, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QFontMetrics, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPlainTextEdit,
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

# 行高自适应：最小行高与文本换行内容上下的留白
_MIN_ROW_HEIGHT = 42
_CELL_V_PAD = 8
# 文本编辑框（QPlainTextEdit）边框 + 文档边距等固定附加高度
_EDITOR_CHROME = 12


def fmt_time(seconds: float) -> str:
    """秒 → "分:秒.十分位"（如 83.46 → "01:23.4"）。"""
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


def extract_clip(audio_wav: str, start: float, end: float) -> "str | None":
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


class SegmentsTableWidget(QWidget):
    """分段表格控件。

    - segments: Segment 对象列表（就地编辑，编辑模式）
    - audio_wav: 整条音轨 wav 路径；选中行后切取该句片段在底部试听条播放
    - readonly: 只读模式无编辑控件，仅展示与试听
    """

    changed = Signal()  # 任意就地编辑（说话人/文本/删除标记）后发射

    def __init__(
        self,
        segments: list,
        audio_wav: str = "",
        readonly: bool = False,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._segments = segments
        self._audio_wav = audio_wav or ""
        self._readonly = readonly
        self._clip_cache: "dict[tuple[float, float], str]" = {}
        self._action_buttons: list[QPushButton] = []
        self._editors: list[QPlainTextEdit] = []

        columns = [
            tr("asr_review.column_index"),
            tr("asr_review.column_start"),
            tr("asr_review.column_end"),
            tr("asr_review.column_speaker"),
            tr("asr_review.column_text"),
        ]
        if not readonly:
            columns += [tr("asr_review.column_status"), tr("asr_review.column_actions")]

        self._table = QTableWidget(len(self._segments), len(columns), self)
        self._table.setHorizontalHeaderLabels(columns)
        self._table.setStyleSheet(TABLE_QSS)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(_MIN_ROW_HEIGHT)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        widths = [(0, 52), (1, 84), (2, 84), (3, 96)]
        if not readonly:
            widths += [(5, 100), (6, 84)]
        for col, width in widths:
            self._table.setColumnWidth(col, width)

        if not self._readonly:
            # 列宽变化（拖拽或窗口缩放）后重算各行的换行行高；拖拽过程中
            # sectionResized 触发频繁，用单发定时器合并
            self._height_timer = QTimer(self)
            self._height_timer.setSingleShot(True)
            self._height_timer.setInterval(40)
            self._height_timer.timeout.connect(self._update_all_row_heights)
            header.sectionResized.connect(lambda *_: self._height_timer.start())

        self._player = AudioPlayerBar(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(self._player)

        self._fill_rows()
        if not self._readonly:
            # 首次布局完成后按最终列宽校准各行高度（Stretch 列宽此时才确定）
            QTimer.singleShot(0, self._update_all_row_heights)

    # -------------------------------------------------------------- 公开接口

    def stop(self) -> None:
        """停止试听播放（窗口关闭前调用）。"""
        self._player.stop()

    def eventFilter(self, obj, event) -> bool:
        """文本编辑框：拦截回车键，保持单句文本单行（不插入换行）。"""
        if (
            event.type() == QEvent.Type.KeyPress
            and event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter)
        ):
            return True
        return super().eventFilter(obj, event)

    # -------------------------------------------------------------- 内部构建

    def _fill_rows(self) -> None:
        """填充表格行：编辑模式的说话人下拉选项 = 全部出现过的 spk 并排序。"""
        speakers = sorted({str(seg.spk) for seg in self._segments})
        center = Qt.AlignmentFlag.AlignCenter
        for row, seg in enumerate(self._segments):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, 0, index_item)
            self._table.setItem(row, 1, QTableWidgetItem(fmt_time(float(seg.start))))
            self._table.setItem(row, 2, QTableWidgetItem(fmt_time(float(seg.end))))

            if self._readonly:
                spk_item = QTableWidgetItem(str(seg.spk))
                spk_item.setTextAlignment(center)
                self._table.setItem(row, 3, spk_item)
                self._table.setItem(row, 4, QTableWidgetItem(str(seg.text)))
            else:
                combo = QComboBox()
                combo.setStyleSheet("QComboBox { min-width: 0; padding: 4px 8px; }")
                combo.addItems(speakers)
                combo.setCurrentText(str(seg.spk))
                combo.currentTextChanged.connect(
                    lambda text, r=row: self._on_spk_changed(r, text)
                )
                self._table.setCellWidget(row, 3, combo)

                # 文本编辑框：自动换行、无滚动条；内容行数变化时行高自适应
                edit = QPlainTextEdit(str(seg.text))
                edit.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
                edit.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                edit.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
                edit.setTabChangesFocus(True)
                edit.installEventFilter(self)  # 拦截回车，避免文本插入换行
                edit.textChanged.connect(lambda r=row: self._on_text_changed(r))
                edit_holder = QWidget()
                holder_layout = QVBoxLayout(edit_holder)
                holder_layout.setContentsMargins(2, 2, 2, 2)
                holder_layout.setSpacing(0)
                holder_layout.addWidget(edit, 0, Qt.AlignmentFlag.AlignVCenter)
                self._editors.append(edit)
                self._table.setCellWidget(row, 4, edit_holder)

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
            if not self._readonly:
                self._update_row_height(row)

    def _apply_row_state(self, row: int) -> None:
        """按 deleted 状态刷新整行外观：置灰 / 恢复。"""
        seg = self._segments[row]
        deleted = bool(seg.deleted)
        for col in (0, 1, 2):
            item = self._table.item(row, col)
            item.setForeground(_COLOR_DELETED if deleted else _COLOR_NORMAL)
            item.setBackground(_COLOR_BG_DELETED if deleted else _COLOR_BG_NORMAL)

        if self._readonly:
            for col in (3, 4):
                item = self._table.item(row, col)
                item.setForeground(_COLOR_DELETED if deleted else _COLOR_NORMAL)
                item.setBackground(_COLOR_BG_DELETED if deleted else _COLOR_BG_NORMAL)
            return

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

    # -------------------------------------------------------------- 行高自适应

    def _wrapped_height(self, text: str, width: int) -> int:
        """估算文本按给定宽度自动换行后所需的高度（像素）。"""
        fm = QFontMetrics(self._table.font())
        avail = max(width - 16, 30)  # 扣除单元格左右留白
        rect = fm.boundingRect(QRect(0, 0, avail, 100000), Qt.TextFlag.TextWordWrap, text)
        return rect.height()

    def _update_row_height(self, row: int) -> None:
        """按文本换行后的高度调整行高，并同步文本编辑框高度。"""
        if self._readonly:
            return
        seg = self._segments[row]
        text_h = self._wrapped_height(str(seg.text or ""), self._table.columnWidth(4))
        editor_h = text_h + _EDITOR_CHROME
        height = max(_MIN_ROW_HEIGHT, editor_h + _CELL_V_PAD)
        self._table.setRowHeight(row, height)
        self._editors[row].setFixedHeight(min(editor_h, height - _CELL_V_PAD))

    def _update_all_row_heights(self) -> None:
        """按当前列宽重算全部行高（列宽变化/首次布局后调用）。"""
        if self._readonly:
            return
        for row in range(len(self._segments)):
            self._update_row_height(row)

    # -------------------------------------------------------------- 内部槽

    def _on_spk_changed(self, row: int, text: str) -> None:
        self._segments[row].spk = text
        self.changed.emit()

    def _on_text_changed(self, row: int) -> None:
        """文本编辑：回写 Segment 并按换行行数刷新行高。"""
        edit = self._editors[row]
        self._segments[row].text = edit.toPlainText()
        self._update_row_height(row)
        self.changed.emit()

    def _toggle_delete_state(self, row: int) -> None:
        """切换某行的删除标记（“删除”/“恢复”按钮共用槽）。"""
        seg = self._segments[row]
        seg.deleted = not bool(seg.deleted)
        self._apply_row_state(row)
        self.changed.emit()

    def _on_selection_changed(self) -> None:
        """选中行后：切取该句片段并在底部试听。"""
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
            self._clip_cache[key] = extract_clip(self._audio_wav, start, end) or ""
        return self._clip_cache[key]
