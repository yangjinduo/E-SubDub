# -*- coding: utf-8 -*-
"""翻译结果人工校对对话框。

逐句核对译文：编辑译文、查看预估朗读匹配提示，并可试听原句人声。
Segment 采用鸭子类型，与 app.core.segment.Segment 完全兼容。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
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
    # 延迟导入：与 asr_review_dialog 保持各自独立实现
    import subprocess
    import tempfile

    from app.core.ffmpeg import get_ffmpeg_path

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


def _estimate_match(text: str, duration: float, target_lang: str) -> "tuple[str, str, str]":
    """粗略估算译文朗读时长与句子可容时长的匹配情况。

    中文≈4.5 字/秒，其他语言≈12 音节/秒；预估朗读时长偏离可容时长
    超过 ±30% 时提示偏长/偏短。返回 (状态文本, 前景色, 提示文本)。
    """
    text = text.strip()
    if not text or duration <= 0:
        return (tr("translation_review.match_pending"), "#9CA3AF", "")
    rate = 4.5 if "中" in target_lang else 12.0
    estimated = len(text) / rate
    tip = tr(
        "translation_review.match_tip",
        chars=len(text),
        estimated=estimated,
        duration=duration,
    )
    if estimated > duration * 1.3:
        return (tr("translation_review.match_long"), "#D97706", tip)
    if estimated < duration * 0.7:
        return (tr("translation_review.match_short"), "#2563EB", tip)
    return (tr("translation_review.match_ok"), "#059669", tip)


class TranslationReviewDialog(QDialog):
    """翻译校对对话框：编辑即时回写 Segment，确认后 get_result() 返回原列表。"""

    def __init__(
        self,
        segments: list,
        source_lang: str = "英语",
        target_lang: str = "中文",
        audio_wav: str = "",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._segments = segments  # 直接持有原列表，就地修改
        self._source_lang = source_lang
        self._target_lang = target_lang
        self._audio_wav = audio_wav or ""
        self._clip_cache: "dict[tuple[float, float], str]" = {}

        self.setWindowTitle(tr("translation_review.title"))
        self.resize(900, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题 + 统计
        title_row = QHBoxLayout()
        title = QLabel(
            tr("translation_review.heading", source=source_lang, target=target_lang)
        )
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
                tr("translation_review.column_index"),
                tr("translation_review.column_time"),
                tr("translation_review.column_speaker"),
                tr("translation_review.column_source"),
                tr("translation_review.column_target"),
                tr("translation_review.column_duration"),
                tr("translation_review.column_match"),
            ]
        )
        self._table.setStyleSheet(TABLE_QSS)
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(42)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(True)
        if self._audio_wav:
            self._table.itemSelectionChanged.connect(self._on_selection_changed)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 46), (1, 130), (2, 70), (5, 64), (6, 96)):
            self._table.setColumnWidth(col, width)
        layout.addWidget(self._table, stretch=1)

        # 底部试听条（audio_wav 为空时保持禁用）
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
        """返回修改后的分段列表（即传入的原列表，译文已就地更新）。"""
        return self._segments

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
            self._table.setItem(row, 3, QTableWidgetItem(str(seg.text)))

            edit = QLineEdit(str(seg.target_text))
            edit.setStyleSheet("QLineEdit { padding: 4px 8px; }")
            edit.textChanged.connect(
                lambda text, r=row: self._on_target_changed(r, text)
            )
            self._table.setCellWidget(row, 4, edit)

            duration_item = QTableWidgetItem(f"{float(seg.duration):.1f}")
            duration_item.setTextAlignment(center)
            self._table.setItem(row, 5, duration_item)

            match_item = QTableWidgetItem()
            match_item.setTextAlignment(center)
            self._table.setItem(row, 6, match_item)

            self._apply_row_state(row)

    def _apply_row_state(self, row: int) -> None:
        """按 deleted 状态刷新整行外观，并重算匹配提示。"""
        seg = self._segments[row]
        deleted = bool(seg.deleted)
        for col in (0, 1, 2, 3, 5):
            item = self._table.item(row, col)
            item.setForeground(_COLOR_DELETED if deleted else _COLOR_NORMAL)
            item.setBackground(_COLOR_BG_DELETED if deleted else _COLOR_BG_NORMAL)

        self._table.cellWidget(row, 4).setEnabled(not deleted)

        match_item = self._table.item(row, 6)
        if deleted:
            match_item.setText("—")
            match_item.setForeground(_COLOR_DELETED)
            match_item.setToolTip(tr("translation_review.deleted_tooltip"))
        else:
            self._update_match_item(row)

    def _update_match_item(self, row: int) -> None:
        """按当前译文重算该行的预估朗读匹配提示。"""
        seg = self._segments[row]
        text, color, tip = _estimate_match(
            str(seg.target_text), float(seg.duration), self._target_lang
        )
        match_item = self._table.item(row, 6)
        match_item.setText(text)
        match_item.setForeground(QColor(color))
        match_item.setToolTip(tip)

    def _refresh_stats(self) -> None:
        """刷新顶部统计标签。"""
        total = len(self._segments)
        deleted = sum(1 for seg in self._segments if seg.deleted)
        self._stats_label.setText(
            tr("translation_review.stats", total=total, deleted=deleted)
        )

    # -------------------------------------------------------------- 内部槽

    def _on_target_changed(self, row: int, text: str) -> None:
        """译文编辑：回写 Segment 并刷新匹配提示。"""
        self._segments[row].target_text = text
        if not self._segments[row].deleted:
            self._update_match_item(row)

    def _on_selection_changed(self) -> None:
        """选中行后：切取该句人声片段并在底部试听。"""
        if not self._audio_wav:
            return
        row = self._table.currentRow()
        if row < 0 or row >= len(self._segments):
            return
        seg = self._segments[row]
        start, end = float(seg.start), float(seg.end)
        key = (round(start, 3), round(end, 3))
        if key not in self._clip_cache:
            self._clip_cache[key] = _extract_clip(self._audio_wav, start, end) or ""
        clip = self._clip_cache[key]
        if clip:
            self._player.set_source(clip)
            self._player.play()
        else:
            self._player.set_source("")
