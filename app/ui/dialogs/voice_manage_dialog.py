# -*- coding: utf-8 -*-
"""说话人音色管理对话框。

按说话人展示卡片：句数统计、参考音频路径、例句预览，支持试听与更换参考音频。
仅展示与更换，不提供删除说话人。Segment 采用鸭子类型。
"""

from __future__ import annotations

import os

from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
from app.ui.style import tokens
from app.ui.widgets.audio_player import AudioPlayerBar

# 例句展示的长度上限，超出部分截断加省略号
_SAMPLE_MAX_CHARS = 80


class VoiceManageDialog(QDialog):
    """说话人音色管理：确认后通过 get_ref_map() 返回可能被更换的映射。"""

    def __init__(self, segments: list, ref_map: dict, sample_map: dict | None = None, parent=None) -> None:
        super().__init__(parent)
        self._segments = segments
        # 拷贝一份：用户取消时不影响外部原映射
        self._ref_map: "dict[str, str]" = dict(ref_map)
        # 参考音频对应分段的原文（挑选阶段记录；用于例句与试听音频严格对照）
        self._sample_map: "dict[str, str]" = dict(sample_map or {})
        # 卡片控件索引：spk -> {"name_label", "path_label", "listen_button"}
        self._cards: "dict[str, dict]" = {}

        self.setWindowTitle(tr("voice.title"))
        self.resize(900, 640)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 16)
        layout.setSpacing(12)

        # 顶部：标题 + 说明
        title = QLabel(tr("voice.heading"))
        title.setObjectName("sectionTitle")
        hint = QLabel(tr("voice.hint"))
        hint.setStyleSheet("color: #6B7280;")
        layout.addWidget(title)
        layout.addWidget(hint)

        # 说话人卡片滚动区
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        container = QWidget()
        container.setStyleSheet("background: transparent;")
        self._cards_layout = QVBoxLayout(container)
        self._cards_layout.setContentsMargins(2, 2, 2, 2)
        self._cards_layout.setSpacing(10)
        scroll.setWidget(container)
        layout.addWidget(scroll, stretch=1)

        # 底部试听条 + 按钮行
        self._player = AudioPlayerBar(self)
        layout.addWidget(self._player)

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

        self._build_cards()

    # -------------------------------------------------------------- 公开接口

    def get_ref_map(self) -> "dict[str, str]":
        """返回可能被更换后的 {spk: 参考音频路径} 映射。"""
        return self._ref_map

    def set_ref_path(self, spk: str, path: str) -> None:
        """更新某说话人的参考音频路径（“更换”按钮选中文件后调用）。"""
        self._ref_map[spk] = path
        self._refresh_card(spk)

    def done(self, result: int) -> None:
        """关闭前释放播放器持有的参考音频句柄（确认后会覆盖写 refs 目录）。"""
        self._player.release()
        super().done(result)

    # -------------------------------------------------------------- 内部构建

    def _build_cards(self) -> None:
        """按说话人（排序后）逐个生成卡片。"""
        for spk in sorted(self._ref_map.keys()):
            self._cards_layout.addWidget(self._build_card(spk))
        self._cards_layout.addStretch(1)

    def _build_card(self, spk: str) -> QFrame:
        """构建单个说话人卡片。"""
        card = QFrame()
        card.setObjectName("resourceCard")
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        row.setSpacing(12)

        # 左侧：说话人 + 参考路径 + 例句
        info = QVBoxLayout()
        info.setSpacing(4)
        name_label = QLabel(
            tr("voice.speaker_count", spk=spk, count=self._count_sentences(spk))
        )
        c = tokens()
        name_label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {c['text_strong']};")
        path_label = QLabel()
        path_label.setStyleSheet(f"color: {c['text_muted']}; font-size: 12px;")
        path_label.setWordWrap(True)
        sample_label = QLabel(tr("voice.sample", text=self._sample_text(spk)))
        sample_label.setStyleSheet(f"color: {c['text_faint']}; font-size: 12px;")
        sample_label.setWordWrap(True)
        info.addWidget(name_label)
        info.addWidget(path_label)
        info.addWidget(sample_label)
        row.addLayout(info, stretch=1)

        # 右侧：试听 / 更换按钮
        listen_button = QPushButton(tr("voice.listen"))
        listen_button.setObjectName("cardSettingButton")
        listen_button.clicked.connect(lambda _=False, s=spk: self._on_listen_clicked(s))
        change_button = QPushButton(tr("voice.change"))
        change_button.setObjectName("cardSettingButton")
        change_button.clicked.connect(lambda _=False, s=spk: self._on_change_clicked(s))
        row.addWidget(listen_button)
        row.addWidget(change_button)

        self._cards[spk] = {
            "name_label": name_label,
            "path_label": path_label,
            "sample_label": sample_label,
            "listen_button": listen_button,
        }
        self._refresh_card(spk)
        return card

    def _refresh_card(self, spk: str) -> None:
        """刷新卡片上的参考路径显示与试听按钮可用状态。"""
        path = self._ref_map.get(spk, "")
        path_label = self._cards[spk]["path_label"]
        path_label.setText(path if path else tr("voice.no_ref"))
        self._cards[spk]["listen_button"].setEnabled(
            bool(path) and os.path.isfile(path)
        )

    def _count_sentences(self, spk: str) -> int:
        """该说话人未删除的句子数。"""
        return sum(
            1 for seg in self._segments if seg.spk == spk and not seg.deleted
        )

    def _sample_text(self, spk: str) -> str:
        """例句 = 参考音频对应分段的原文（与试听音频严格对照）。

        无记录时（如外部更换了参考音频）回退显示该说话人最长的一句，
        此时例句仅代表说话人内容，不保证与试听音频对应。
        """
        text = str(self._sample_map.get(spk, "") or "").strip()
        if not text:
            candidates = [
                seg for seg in self._segments
                if seg.spk == spk and not seg.deleted and str(seg.text)
            ]
            if not candidates:
                return tr("voice.no_sample")
            text = str(max(candidates, key=lambda seg: len(str(seg.text))).text)
        if len(text) > _SAMPLE_MAX_CHARS:
            return text[:_SAMPLE_MAX_CHARS] + "…"
        return text

    # -------------------------------------------------------------- 内部槽

    def _on_listen_clicked(self, spk: str) -> None:
        """试听该说话人的参考音频。"""
        path = self._ref_map.get(spk, "")
        self._player.set_source(path)
        self._player.play()

    def _on_change_clicked(self, spk: str) -> None:
        """弹出文件对话框更换该说话人的参考音频。"""
        path, _ = QFileDialog.getOpenFileName(
            self,
            tr("voice.choose_ref_title", spk=spk),
            "",
            f"{tr('voice.filter_audio')} (*.wav);;{tr('voice.filter_all')} (*)",
        )
        if path:
            self.set_ref_path(spk, path)
