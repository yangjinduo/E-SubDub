# -*- coding: utf-8 -*-
"""情绪定制对话框（文本转语音任务）：逐句选择合成情绪。

表格三列：序号 / 内容 / 情绪；分句与合成单位一致（split_sentences）。
情绪默认"跟随参考音频"（emo_vector=None，继承参考音频的语气情绪）；
各情绪选项以不同颜色显示，表格内一目了然。

情绪 key 与 app/core/tts_runner.py 的 EMOTION_KEYS 对应
（IndexTTS-2.5 的 8 维情绪向量顺序）。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr

# 情绪 key → (i18n key, 显示颜色)。None = 跟随参考音频（默认）
EMOTION_ITEMS: list[tuple] = [
    (None, "emo.follow", "#0D9488"),
    ("happy", "emo.happy", "#F59E0B"),
    ("calm", "emo.calm", "#6B7280"),
    ("sad", "emo.sad", "#3B82F6"),
    ("angry", "emo.angry", "#DC2626"),
    ("afraid", "emo.afraid", "#8B5CF6"),
    ("disgusted", "emo.disgusted", "#10B981"),
    ("melancholic", "emo.melancholic", "#6366F1"),
    ("surprised", "emo.surprised", "#EA580C"),
]


def emotion_label(key) -> str:
    """情绪 key → 界面显示文本（None = 跟随参考音频）。"""
    for item_key, i18n_key, _color in EMOTION_ITEMS:
        if item_key == key:
            return tr(i18n_key)
    return tr("emo.follow")


def emotion_color(key) -> str:
    """情绪 key → 显示颜色。"""
    for item_key, _i18n_key, color in EMOTION_ITEMS:
        if item_key == key:
            return color
    return "#0D9488"


class _EmotionCombo(QComboBox):
    """情绪选择下拉：当前选项以对应颜色显示。"""

    def __init__(self, sentences_key: str, current, parent=None) -> None:
        super().__init__(parent)
        self._row_text = sentences_key
        for item_key, i18n_key, color in EMOTION_ITEMS:
            self.addItem(tr(i18n_key), item_key)
            # 下拉列表项文本着色（弹出菜单内一目了然）
            self.setItemData(
                self.count() - 1, color, Qt.ItemDataRole.ForegroundRole,
            )
        index = next(
            (i for i, (k, _ik, _c) in enumerate(EMOTION_ITEMS) if k == current),
            0,
        )
        self.setCurrentIndex(index)
        self._apply_color()
        self.currentIndexChanged.connect(lambda _i: self._apply_color())

    def _apply_color(self) -> None:
        """当前情绪的文本颜色应用到下拉框（表格内快速识别）。"""
        color = emotion_color(self.currentData())
        self.setStyleSheet(
            f"QComboBox {{ color: {color}; font-weight: 600; }}"
        )

    def emotion_key(self):
        return self.currentData()


class TtsEmotionDialog(QDialog):
    """逐句情绪定制（模态）。确定后 get_emotions() 返回 {句子: 情绪 key}。"""

    def __init__(
        self, sentences: list[str], emotions: dict, parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("emo_dialog.title"))
        self.setMinimumSize(720, 480)
        self.resize(760, 560)
        self._sentences = sentences
        self._combos: list[_EmotionCombo] = []

        tip = QLabel(tr("emo_dialog.tip"))
        tip.setStyleSheet("color: #6B7280;")
        tip.setWordWrap(True)

        self._table = QTableWidget(len(sentences), 3, self)
        self._table.setHorizontalHeaderLabels([
            tr("emo_dialog.col.index"),
            tr("emo_dialog.col.text"),
            tr("emo_dialog.col.emotion"),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch,
        )
        self._table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents,
        )
        for row, sentence in enumerate(sentences):
            index = QTableWidgetItem(str(row + 1))
            index.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            text = QTableWidgetItem(sentence)
            text.setToolTip(sentence)
            combo = _EmotionCombo(sentence, emotions.get(sentence), self._table)
            self._combos.append(combo)
            self._table.setItem(row, 0, index)
            self._table.setItem(row, 1, text)
            self._table.setCellWidget(row, 2, combo)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(tip)
        layout.addWidget(self._table, 1)
        layout.addWidget(buttons)

    def get_emotions(self) -> dict:
        """确定后的情绪映射：{句子文本: 情绪 key}（跟随参考音频的句子不记）。"""
        emotions: dict = {}
        for sentence, combo in zip(self._sentences, self._combos):
            key = combo.emotion_key()
            if key is not None:
                emotions[sentence] = key
        return emotions
