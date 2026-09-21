# -*- coding: utf-8 -*-
"""TTS 配音结果复核对话框。

针对 needs_review 的句子：试听生成的配音、修改译文、勾选需要重生成的句子。
Segment 采用鸭子类型，与 app.core.segment.Segment 完全兼容。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
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


class TtsReviewDialog(QDialog):
    """TTS 配音复核对话框。

    勾选需重生成的句子（get_regenerate_indices），译文编辑即时回写
    （get_edited_segments 返回传入的原列表）。
    """

    def __init__(self, segments: list, parent=None) -> None:
        super().__init__(parent)
        self._segments = segments  # needs_review=True 的句子列表，就地修改
        self._regen_flags: list[bool] = [False] * len(segments)
        self._checkboxes: list[QCheckBox] = []
        self._listen_buttons: list[QPushButton] = []

        self.setWindowTitle(tr("tts_review.title"))
        self.resize(900, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题 + 统计 + 全部重生成快捷按钮
        title_row = QHBoxLayout()
        title = QLabel(tr("tts_review.heading"))
        title.setObjectName("sectionTitle")
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #6B7280;")
        self._select_all_button = QPushButton(tr("tts_review.regenerate_all"))
        self._select_all_button.setObjectName("cardSettingButton")
        self._select_all_button.clicked.connect(self._on_select_all)
        title_row.addWidget(title)
        title_row.addStretch(1)
        title_row.addWidget(self._stats_label)
        title_row.addWidget(self._select_all_button)
        layout.addLayout(title_row)

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
        for col, width in ((0, 46), (1, 130), (2, 70), (4, 70), (5, 70), (6, 92)):
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

    def get_regenerate_indices(self) -> "list[int]":
        """返回勾选了“重新生成此句”的行索引（相对传入列表）。"""
        return [i for i, flag in enumerate(self._regen_flags) if flag]

    def get_edited_segments(self) -> list:
        """返回包含译文修改的分段列表（即传入的原列表，已就地更新）。"""
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

            edit = QLineEdit(str(seg.target_text))
            edit.setStyleSheet("QLineEdit { padding: 4px 8px; }")
            edit.textChanged.connect(
                lambda text, r=row: self._on_target_changed(r, text)
            )
            self._table.setCellWidget(row, 3, edit)

            ratio_item = QTableWidgetItem(_fmt_ratio(seg))
            ratio_item.setTextAlignment(center)
            self._table.setItem(row, 4, ratio_item)

            # 试听按钮：tts_audio_path 为空或文件不存在时禁用
            path = str(getattr(seg, "tts_audio_path", "") or "")
            listen_button = QPushButton(tr("tts_review.listen"))
            listen_button.setObjectName("cardSettingButton")
            listen_button.setEnabled(bool(path) and os.path.isfile(path))
            listen_button.clicked.connect(lambda _=False, p=path: self._on_listen_clicked(p))
            self._listen_buttons.append(listen_button)
            listen_holder = QWidget()
            listen_layout = QHBoxLayout(listen_holder)
            listen_layout.setContentsMargins(6, 2, 6, 2)
            listen_layout.addWidget(listen_button)
            self._table.setCellWidget(row, 5, listen_holder)

            # 重新生成勾选框
            checkbox = QCheckBox(tr("tts_review.regenerate_this"))
            checkbox.toggled.connect(
                lambda checked, r=row: self._on_regen_toggled(r, checked)
            )
            self._checkboxes.append(checkbox)
            self._table.setCellWidget(row, 6, checkbox)

    def _refresh_stats(self) -> None:
        """刷新顶部统计标签。"""
        total = len(self._segments)
        checked = sum(1 for flag in self._regen_flags if flag)
        self._stats_label.setText(
            tr("tts_review.stats", total=total, checked=checked)
        )

    # -------------------------------------------------------------- 内部槽

    def _on_target_changed(self, row: int, text: str) -> None:
        """译文编辑：即时回写 Segment。"""
        self._segments[row].target_text = text

    def _on_regen_toggled(self, row: int, checked: bool) -> None:
        """勾选状态变化：记录并刷新统计。"""
        self._regen_flags[row] = bool(checked)
        self._refresh_stats()

    def _on_select_all(self) -> None:
        """快捷按钮：全选 / 取消全选“重新生成”勾选框。"""
        target = not all(self._regen_flags)
        for checkbox in self._checkboxes:
            checkbox.setChecked(target)

    def _on_listen_clicked(self, path: str) -> None:
        """试听该句生成的配音。"""
        self._player.set_source(path)
        self._player.play()
