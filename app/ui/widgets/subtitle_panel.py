# -*- coding: utf-8 -*-
"""字幕显示面板：逐条列出成品 SRT（左列时间 + 右列字幕），高亮跟随播放位置。

读取封装输出夹里的成品 SRT（多份时取修改时间最新的一份），每条 cue 一行
两列：时间列显示该句起止区间，字幕列显示对白文本（双语 cue 两种语言分行
显示：第一行/第二行）；QTimer(100ms) 轮询已注册播放器的 position/
is_playing，正在播放的播放器优先（多播放器互斥场景下谁在播放跟随谁），
命中区间即高亮该行并滚动到列表垂直中央；都停止时保持最后高亮位置。

高亮通过选中态实现（selectRow + QSS selection-background-color）：QSS
规则一旦作用于 item，会覆盖 item 的 Foreground/Background 角色画刷，
直接 setForeground/setBackground 做高亮不可见。
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QTextLayout, QTextOption
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHeaderView,
    QLabel,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from app.core.i18n import tr
from app.ui.style import tokens


def _table_qss() -> str:
    """字幕表格 QSS（按当前主题取色，与全局 QSS 保持一致观感）。"""
    c = tokens()
    return f"""
QTableWidget {{
    background: {c['table_bg']};
    border: 1px solid {c['table_border']};
    border-radius: 10px;
    gridline-color: {c['table_gridline']};
    outline: none;
    selection-background-color: {c['table_sel_bg']};
    selection-color: {c['table_sel_text']};
}}
QTableWidget::item {{
    padding: 8px 6px;
}}
"""

# 行高自适应：最小行高（对齐 TtsVoiceReviewDialog）与安全余量；文本
# 高度自算时的宽度内缩与上下补白。宽度内缩必须显著大于 delegate 绘制
# 的全部水平内缩（QSS ::item 左右 padding 12 + 焦点边距 6 + 杂项）：
# 若只小 1~2px，临界词换行落入差异窗口时自算行数比实际绘制少一行，
# 最后一行（双语第二语言行）就被省略号截断 —— 列宽缩放中省略号断续
# 出现/消失正是该窗口被断续命中；取 40 确保计算换行宽度恒窄于绘制，
# 行数只多不少，宁高勿截。
_MIN_ROW_HEIGHT = 42
_ROW_HEIGHT_MARGIN = 4
_TEXT_WIDTH_INSET = 86
_CELL_V_PAD = 24

# 列号与时间列宽（"01:23.4 - 01:25.9" 级别的起止区间）
_COL_TIME, _COL_TEXT = 0, 1
_TIME_COL_WIDTH = 124

# SRT 时间轴行：00:00:01,600 --> 00:00:04,200（毫秒分隔符 , 或 .）
_TIME_RE = re.compile(
    r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)"
)


def _ts_to_ms(h: str, m: str, s: str, ms: str) -> int:
    return ((int(h) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0")[:3])


def _read_text(path: Path) -> str:
    """读字幕文本：优先 UTF-8（含 BOM），失败兜底 GBK。"""
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return path.read_text(encoding="gbk", errors="replace")


def parse_srt(path: Path | str) -> list[tuple[int, int, str]]:
    """解析 SRT → [(start_ms, end_ms, text)]；text 为 cue 内多行以
    换行合并（双语 cue 两种语言分行显示）；格式损坏时返回空列表。"""
    path = Path(path)
    try:
        content = _read_text(path)
    except OSError:
        return []
    cues: list[tuple[int, int, str]] = []
    lines = content.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    i = 0
    while i < len(lines):
        match = _TIME_RE.search(lines[i])
        if match is None:
            i += 1
            continue
        g = match.groups()
        start = _ts_to_ms(*g[:4])
        end = _ts_to_ms(*g[4:])
        i += 1
        text_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            text_lines.append(lines[i].strip())
            i += 1
        text = "\n".join(t for t in text_lines if t)
        if text and end > start:
            cues.append((start, end, text))
    return cues


def _fmt_ts(ms: int) -> str:
    """毫秒 → "分:秒.十分位"（如 83460 → "01:23.4"）。"""
    if ms < 0:
        ms = 0
    seconds = ms / 1000.0
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


def latest_srt_in(folder: Path | str) -> Path | None:
    """目录内修改时间最新的一份 SRT（无则 None）。"""
    folder = Path(folder)
    if not folder.is_dir():
        return None
    candidates = sorted(
        (p for p in folder.glob("*.srt") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


class SubtitlePanel(QFrame):
    """字幕显示面板：SRT 逐条列表（时间列 + 字幕列）+ 播放位置高亮。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._cues: list[tuple[int, int, str]] = []
        self._players: list = []       # is_playing()/position() 鸭子类型
        self._highlight_row = -1       # 当前高亮行（-1 无）

        title = QLabel(tr("viewer.subtitle_panel"))
        title.setObjectName("viewerTrackTitle")

        self._hint_label = QLabel(tr("viewer.subtitle_none"))
        self._hint_label.setObjectName("viewerHint")
        self._hint_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_label.setWordWrap(True)

        self._table = QTableWidget(self)
        self._table.setColumnCount(2)
        self._table.setStyleSheet(_table_qss())
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setVisible(False)
        self._table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self._table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self._table.setWordWrap(True)
        # NoFocus 去掉焦点框，点击行形成的选中会在下次轮询被拉回播放行
        self._table.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._table.setVerticalScrollMode(
            QTableWidget.ScrollMode.ScrollPerPixel
        )
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header.setSectionResizeMode(_COL_TEXT, QHeaderView.ResizeMode.Stretch)
        self._table.setColumnWidth(_COL_TIME, _TIME_COL_WIDTH)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(title)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._hint_label)
        self._set_loaded(False)

        # 字幕列宽变化后重算各行换行行高；拖拽过程中用单发定时器合并
        self._height_timer = QTimer(self)
        self._height_timer.setSingleShot(True)
        self._height_timer.setInterval(40)
        self._height_timer.timeout.connect(self._update_all_row_heights)
        header.sectionResized.connect(lambda *_: self._height_timer.start())

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_tick)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 首次/再次显示时列宽才随布局最终确定，补一次行高计算
        if self._cues:
            self._height_timer.start()

    # -------------------------------------------------------------- 公开接口

    def set_players(self, players: list) -> None:
        """注册跟随的播放器列表（谁在播放跟随谁）。"""
        self._players = [p for p in players if p is not None]
        if self._cues:
            self._timer.start()

    def load_srt(self, path: Path | str) -> bool:
        """加载 SRT 文件；成功返回 True（无 cue 视为失败）。"""
        cues = parse_srt(path)
        self._cues = cues
        self._table.setRowCount(len(cues))
        center = Qt.AlignmentFlag.AlignCenter
        for row, (start, end, text) in enumerate(cues):
            time_item = QTableWidgetItem(f"{_fmt_ts(start)} - {_fmt_ts(end)}")
            time_item.setTextAlignment(center)
            time_item.setForeground(QColor(tokens()["text_muted"]))
            self._table.setItem(row, _COL_TIME, time_item)

            text_item = QTableWidgetItem(text)
            text_item.setToolTip(text)
            self._table.setItem(row, _COL_TEXT, text_item)
        self._highlight_row = -1
        self._table.clearSelection()
        loaded = bool(cues)
        self._set_loaded(loaded)
        if loaded:
            QTimer.singleShot(0, self._update_all_row_heights)
            if self._players:
                self._timer.start()
        else:
            self._timer.stop()
        return loaded

    def load_latest_srt_from(self, folder: Path | str) -> bool:
        """加载目录内最新的一份 SRT。"""
        path = latest_srt_in(folder)
        return self.load_srt(path) if path is not None else self.load_srt("")

    def clear(self) -> None:
        self._timer.stop()
        self._cues = []
        self._table.setRowCount(0)
        self._highlight_row = -1
        self._set_loaded(False)

    # -------------------------------------------------------------- 内部

    def _set_loaded(self, loaded: bool) -> None:
        self._table.setVisible(loaded)
        self._hint_label.setVisible(not loaded)

    def _text_height(self, text: str, width: int) -> int:
        """QTextLayout 按给定宽度布局文本，返回所需内容高度（像素）。

        与 delegate 绘制用同一布局引擎（WordWrap + 显式 \n 硬换行），
        双语 cue 的第二行与自动换行的末行都能算全 —— delegate 的
        sizeHint 对含 \n 的文本会低估高度（第二行被省略号截断）。
        """
        layout = QTextLayout(text, self._table.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapMode.WordWrap)
        layout.setTextOption(option)
        layout.beginLayout()
        height = 0.0
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            height += line.height()
        layout.endLayout()
        return int(height + 0.5)

    def _update_all_row_heights(self) -> None:
        """按当前列宽自适应全部行高（列宽变化/加载/显示后调用）。

        双保险：先 resizeRowsToContents（delegate sizeHint，含 QSS
        padding），再对字幕列逐行用 QTextLayout 自算高度取更大者 ——
        delegate 对含显式换行的双语文本会低估，仅靠它会截断第二行。
        """
        self._table.resizeRowsToContents()
        width = max(self._table.columnWidth(_COL_TEXT) - _TEXT_WIDTH_INSET, 30)
        for row in range(self._table.rowCount()):
            height = self._table.rowHeight(row) + _ROW_HEIGHT_MARGIN
            item = self._table.item(row, _COL_TEXT)
            if item is not None and item.text():
                height = max(height,
                             self._text_height(item.text(), width)
                             + _CELL_V_PAD + _ROW_HEIGHT_MARGIN)
            self._table.setRowHeight(row, max(_MIN_ROW_HEIGHT, height))

    def _on_tick(self) -> None:
        position = None
        for player in self._players:
            try:
                if player.is_playing():
                    position = int(player.position())
                    break
            except Exception:
                continue
        if position is None:
            return  # 都停止：保持最后高亮位置
        self._highlight_at(position)

    def _highlight_at(self, position_ms: int) -> None:
        """高亮播放时间对应的字幕行并滚动到垂直中央（命中失败保持现状）。"""
        starts = [c[0] for c in self._cues]
        # 二分找最后一个 start <= position 的 cue，再校验区间
        idx = bisect.bisect_right(starts, position_ms) - 1
        row = idx if idx >= 0 and self._cues[idx][1] >= position_ms else -1
        if row < 0 or row == self._highlight_row:
            return
        self._highlight_row = row
        # SelectRows 下 selectRow 即整行选中 → QSS selection 高亮
        self._table.selectRow(row)
        index = self._table.model().index(row, _COL_TIME)
        if index.isValid():
            self._table.scrollTo(
                index, QAbstractItemView.ScrollHint.PositionAtCenter
            )
