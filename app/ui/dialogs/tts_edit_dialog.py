# -*- coding: utf-8 -*-
"""编辑对话框（文本转语音任务）：改写句子 + 逐句选择情绪 + 删除句子。

表格四列：序号 / 内容（可直接修改文本）/ 情绪 / 删除。分句与合成单位
一致（split_sentences）。情绪默认"跟随参考音频"（emo_vector=None，
继承参考音频的语气情绪）；各情绪选项以不同颜色显示。删除为可恢复的
标记（行置灰），确定后 get_result() 返回保留的句子列表与情绪映射，
由窗口层回写输入区（手动模式回输入框，文件模式改内存缓存）。

情绪 key 与 app/core/tts_runner.py 的 EMOTION_KEYS 对应
（IndexTTS-2.5 的 8 维情绪向量顺序）。
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
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

    def __init__(self, current, parent=None) -> None:
        super().__init__(parent)
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


class TtsEditDialog(QDialog):
    """编辑句子（模态）：改写文本、选情绪、删除句子（可恢复）。

    确定后 get_result() 返回 (保留句子列表, {句子: 情绪 key})；
    get_emotions() 兼容旧接口，返回按保留句子过滤的情绪映射。
    """

    def __init__(
        self, sentences: list[str], emotions: dict, parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("edit_dialog.title"))
        self.setMinimumSize(760, 480)
        self.resize(820, 580)
        self._sentences = list(sentences)
        self._deleted: set[int] = set()  # 已删除行号（可恢复标记）
        self._combos: list[_EmotionCombo] = []

        tip = QLabel(tr("edit_dialog.tip"))
        tip.setStyleSheet("color: #6B7280;")
        tip.setWordWrap(True)

        self._table = QTableWidget(len(self._sentences), 4, self)
        self._table.setHorizontalHeaderLabels([
            tr("edit_dialog.col.index"),
            tr("edit_dialog.col.text"),
            tr("edit_dialog.col.emotion"),
            tr("edit_dialog.col.delete"),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.setWordWrap(True)
        # 内容列可编辑：双击 / 选中后直接输入
        self._table.setEditTriggers(
            QTableWidget.EditTrigger.DoubleClicked
            | QTableWidget.EditTrigger.EditKeyPressed
            | QTableWidget.EditTrigger.AnyKeyPressed
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        for row, sentence in enumerate(self._sentences):
            self._fill_row(row, sentence, emotions.get(sentence))

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

    # ------------------------------------------------------------------ 内部

    def _fill_row(self, row: int, sentence: str, emotion_key) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        index = QTableWidgetItem(str(row + 1))
        index.setTextAlignment(center)
        index.setFlags(
            index.flags() & ~Qt.ItemFlag.ItemIsEditable
        )
        text = QTableWidgetItem(sentence)
        text.setToolTip(sentence)
        # 编辑结束即按新文本自适应行高
        combo = _EmotionCombo(emotion_key, self._table)
        self._combos.append(combo)
        delete = QPushButton(tr("common.delete"))
        delete.setObjectName("rowDeleteButton")
        delete.setFixedHeight(26)
        delete.clicked.connect(
            lambda _checked=False, r=row: self._toggle_deleted(r)
        )
        self._table.setItem(row, 0, index)
        self._table.setItem(row, 1, text)
        self._table.setCellWidget(row, 2, combo)
        self._table.setCellWidget(row, 3, delete)
        self._table.resizeRowToContents(row)

    def _toggle_deleted(self, row: int) -> None:
        """删除/恢复切换：已删除行置灰、按钮变恢复，再点一次还原。"""
        button = self._table.cellWidget(row, 3)
        if row in self._deleted:
            self._deleted.discard(row)
            if button is not None:
                button.setText(tr("common.delete"))
            self._set_row_gray(row, False)
        else:
            self._deleted.add(row)
            if button is not None:
                button.setText(tr("edit_dialog.restore"))
            self._set_row_gray(row, True)

    def _set_row_gray(self, row: int, gray: bool) -> None:
        """置灰/恢复一行（文本与序号变灰、内容列禁编辑、按钮禁用情绪）。"""
        disabled = QColor("#9CA3AF")
        enabled = QColor("#111827")
        text_item = self._table.item(row, 1)
        index_item = self._table.item(row, 0)
        for item in (text_item, index_item):
            if item is not None:
                item.setForeground(disabled if gray else enabled)
        if text_item is not None:
            flags = text_item.flags()
            if gray:
                flags &= ~Qt.ItemFlag.ItemIsEditable
            else:
                flags |= Qt.ItemFlag.ItemIsEditable
            text_item.setFlags(flags)
            if gray:
                text_item.setText(
                    f"{text_item.text()}\n{tr('edit_dialog.deleted')}"
                    if not text_item.text().endswith(
                        tr("edit_dialog.deleted")
                    ) else text_item.text()
                )
        combo = self._table.cellWidget(row, 2)
        if combo is not None:
            combo.setEnabled(not gray)
        if gray and text_item is not None:
            # 折叠“（已删除）”标记：换行追加，行高自适应
            self._table.resizeRowToContents(row)
        else:
            self._table.resizeRowToContents(row)

    # ------------------------------------------------------------------ 结果

    def get_result(self) -> tuple[list[str], dict]:
        """确定后的编辑结果：(保留句子列表, {句子: 情绪 key})。

        删除标记的句子不进入结果；情绪映射以最终文本为键（改写文本后
        的情绪跟随该行下拉选择，跟参考音频的句子不记入映射）。
        """
        kept: list[str] = []
        emotions: dict = {}
        for row, sentence in enumerate(self._sentences):
            if row in self._deleted:
                continue
            item = self._table.item(row, 1)
            text = item.text().strip() if item is not None else sentence
            if not text:
                continue
            kept.append(text)
            key = self._combos[row].emotion_key()
            if key is not None:
                emotions[text] = key
        return kept, emotions

    def get_emotions(self) -> dict:
        """兼容旧接口：按保留句子返回情绪映射。"""
        _kept, emotions = self.get_result()
        return emotions
