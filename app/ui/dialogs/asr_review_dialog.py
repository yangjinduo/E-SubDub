# -*- coding: utf-8 -*-
"""ASR 识别结果人工校对对话框。

逐句核对：修改说话人与文本、标记删除/恢复，并按句切取人声片段试听。
表格与试听条抽取为共用控件 app.ui.widgets.segments_table.SegmentsTableWidget，
本对话框仅保留标题统计、确认/取消按钮等外壳。
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout

from app.core.i18n import tr
from app.ui.widgets.segments_table import SegmentsTableWidget


class AsrReviewDialog(QDialog):
    """ASR 结果校对对话框。

    编辑即时回写传入的 Segment 对象属性，确认后 get_result() 返回同一列表。
    """

    def __init__(self, segments: list, audio_wav: str, parent=None) -> None:
        super().__init__(parent)
        self._segments = segments  # 直接持有原列表，就地修改

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

        # 主表格 + 底部试听条（共用控件，编辑模式）
        self._table_widget = SegmentsTableWidget(segments, audio_wav, readonly=False, parent=self)
        self._table_widget.changed.connect(self._refresh_stats)
        layout.addWidget(self._table_widget, stretch=1)

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

        self._refresh_stats()

    # -------------------------------------------------------------- 公开接口

    def get_result(self) -> list:
        """返回修改后的分段列表（即传入的原列表，属性已就地更新）。"""
        return self._segments

    # -------------------------------------------------------------- 内部

    def _refresh_stats(self) -> None:
        """刷新顶部统计标签。"""
        total = len(self._segments)
        deleted = sum(1 for seg in self._segments if seg.deleted)
        speakers = {str(seg.spk) for seg in self._segments}
        self._stats_label.setText(
            tr("asr_review.stats", total=total, deleted=deleted, speakers=len(speakers))
        )
