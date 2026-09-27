"""中部导入区：支持拖入/点击选择多个文件，导入后以表格展示。

表格列：序号 / 文件名 / 文件路径 / 类型 / 时长 / 删除。
- 点击空白或拖入文件继续追加（不再替换）；
- 防重：文件名与路径均相同视为同一文件，提示重复并跳过
  （同名但路径不同的文件允许加入）；
- 行删除按钮 + 独立"删除所选"按钮，删除前均弹确认对话框；
- 表格支持 Shift 连选、Ctrl 多选、鼠标拖拽框选。
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.ffmpeg import get_ffmpeg_path
from app.core.i18n import tr

# 支持的文件后缀 → 类型显示名
VIDEO_SUFFIXES = {".mkv": "MKV", ".mp4": "MP4", ".mov": "MOV", ".avi": "AVI"}
AUDIO_SUFFIXES = {".wav": "WAV", ".mp3": "MP3", ".flac": "FLAC", ".m4a": "M4A"}

# ffmpeg -i 输出中的时长行，如 "  Duration: 00:01:23.45, start: ..."
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def _file_filter() -> str:
    """文件选择对话框过滤器（按当前界面语言生成标签）。"""
    video = " ".join(f"*{s}" for s in sorted(VIDEO_SUFFIXES))
    audio = " ".join(f"*{s}" for s in sorted(AUDIO_SUFFIXES))
    return (
        f"{tr('import.filter_media')} ({video} {audio});;"
        f"{tr('import.filter_video')} ({video});;"
        f"{tr('import.filter_audio')} ({audio});;"
        f"{tr('import.filter_all')} (*.*)"
    )


def _format_duration(total_seconds: float) -> str:
    """把秒数格式化为 mm:ss 或 h:mm:ss。"""
    total_seconds = int(round(total_seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _probe_duration(path: str) -> str:
    """调用内置 ffmpeg 读取文件时长，失败返回"未知"。"""
    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        return tr("import.duration_unknown")

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW

    try:
        # 不指定输出文件，ffmpeg 会打印媒体信息到 stderr 后以非零码退出，属正常行为
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-i", path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=creation_flags,
        )
    except (OSError, subprocess.SubprocessError):
        return tr("import.duration_unknown")

    match = _DURATION_RE.search(result.stderr or "")
    if not match:
        return tr("import.duration_unknown")
    hours, minutes, seconds = (float(g) for g in match.groups())
    return _format_duration(hours * 3600 + minutes * 60 + seconds)


def _file_kind_label(path: str) -> str:
    """按后缀推断文件类型描述。"""
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return tr("import.kind.video", fmt=VIDEO_SUFFIXES[suffix])
    if suffix in AUDIO_SUFFIXES:
        return tr("import.kind.audio", fmt=AUDIO_SUFFIXES[suffix])
    return tr("import.kind.unknown")


def _dedup_key(path: str) -> str:
    """防重键：规范化后的绝对路径（Windows 下不区分大小写）。

    文件名与路径均相同 → 同一文件；仅文件名相同（路径不同）不加干预。
    """
    return os.path.normcase(str(Path(path).resolve()))


class ImportArea(QFrame):
    """大区域导入控件：拖拽或点击导入多个媒体文件，表格展示。"""

    # 新增文件后发射（参数为本次新加入的路径列表，已去重）
    files_added = Signal(list)
    # 文件列表内容变化（新增/删除）后发射，供外部刷新按钮文案等
    files_changed = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("importDropArea")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(180)

        # 有序文件列表 + 元数据缓存（路径 → {kind, duration}）
        self._files: list[str] = []
        self._meta: dict[str, dict[str, str]] = {}

        # 引导文案（无文件时居中显示）
        self._hint_title = QLabel(tr("import.hint_title"))
        self._hint_title.setObjectName("importHintTitle")
        self._hint_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_sub = QLabel(tr("import.hint_sub"))
        self._hint_sub.setObjectName("importHintSub")
        self._hint_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_ext = QLabel(tr("import.hint_ext"))
        self._hint_ext.setObjectName("importHintSub")
        self._hint_ext.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 有文件时的顶部行文案：已导入计数（与"删除所选"按钮同行）
        self._count_label = QLabel(tr("import.imported_count_hint", count=0))
        self._count_label.setObjectName("importCountLabel")
        self._count_label.hide()

        # 文件表格（列：序号/文件名/文件路径/类型/时长/删除）
        self._table = QTableWidget(0, 6)
        self._table.setObjectName("importFileTable")
        self._table.setHorizontalHeaderLabels([
            tr("import.col.index"), tr("import.col.name"),
            tr("import.col.path"), tr("import.col.type"),
            tr("import.col.duration"), tr("import.col.delete"),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 扩展选择：支持 Shift 连选、Ctrl 多选、鼠标拖拽框选
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setDefaultSectionSize(36)
        # 表格区恢复标准箭头光标（导入区整体为手型，提示可点击）
        self._table.setCursor(Qt.CursorShape.ArrowCursor)
        self._table.hide()

        # 独立"删除所选"按钮（与计数文案同行，置于右上角）
        self._delete_selected_button = QPushButton(tr("import.delete_selected"))
        self._delete_selected_button.setObjectName("dangerButton")
        self._delete_selected_button.clicked.connect(self._delete_selected)
        self._delete_selected_button.hide()

        # 顶部行：已导入计数（左） + 删除所选按钮（右），随表格显隐
        header_row = QHBoxLayout()
        header_row.setContentsMargins(0, 0, 0, 0)
        header_row.setSpacing(12)
        header_row.addWidget(self._count_label)
        header_row.addStretch(1)
        header_row.addWidget(self._delete_selected_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        # 无文件时上下弹簧（均分剩余空间）把提示文案垂直居中；有文件时
        # 弹簧隐藏（隐藏控件不参与布局分配），表格独占全部剩余高度。
        # 注：不用 addStretch —— spacer 无法隐藏，会与表格抢剩余空间
        self._top_spacer = QWidget()
        layout.addWidget(self._top_spacer, 1)
        layout.addWidget(self._hint_title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._hint_sub, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._hint_ext, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addLayout(header_row)
        layout.addWidget(self._table, 1)
        self._bottom_spacer = QWidget()
        layout.addWidget(self._bottom_spacer, 1)

    # ------------------------------------------------------------------ 属性

    @property
    def current_file(self) -> str | None:
        """当前列表第一个文件（兼容单文件语义；未导入为 None）。"""
        return self._files[0] if self._files else None

    def files(self) -> list[str]:
        """已导入的文件路径列表（按导入顺序）。"""
        return list(self._files)

    # ------------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:
        """点击空白区域弹出文件选择框（可多选）。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._choose_files()
        super().mousePressEvent(event)

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("import.choose_title"), str(Path.home()), _file_filter()
        )
        if paths:
            self.add_files(paths)

    # ------------------------------------------------------------------ 文件管理

    def add_files(self, paths: list[str]) -> None:
        """批量追加文件：防重（文件名+路径相同视为重复），重复项弹提示。"""
        existing = {_dedup_key(p) for p in self._files}
        new_files: list[str] = []
        duplicates: list[str] = []
        for path in paths:
            key = _dedup_key(path)
            if key in existing:
                duplicates.append(path)
                continue
            existing.add(key)
            new_files.append(path)

        if duplicates:
            QMessageBox.information(
                self, tr("import.duplicate_title"),
                tr("import.duplicate_found",
                   count=len(duplicates),
                   files="\n".join(f"· {Path(p).name}" for p in duplicates)),
            )
        if not new_files:
            return

        for path in new_files:
            self._files.append(path)
            self._meta[path] = {
                "kind": _file_kind_label(path),
                "duration": _probe_duration(path),
            }
        self._refresh()
        self.files_added.emit(new_files)
        self.files_changed.emit()

    def set_file(self, path: str) -> None:
        """以单个文件替换当前列表（绑定已有任务/切换批量当前文件时使用）。"""
        if self._files == [path]:
            return
        self._files = [path] if path else []
        if path and path not in self._meta:
            self._meta[path] = {
                "kind": _file_kind_label(path),
                "duration": _probe_duration(path),
            }
        self._refresh()
        self.files_changed.emit()

    def _delete_one(self, path: str) -> None:
        """行删除按钮：确认后从列表移除该文件。"""
        name = Path(path).name
        box = QMessageBox(self)
        box.setWindowTitle(tr("import.confirm_delete_title"))
        box.setText(tr("import.confirm_delete_files", count=1, files=f"· {name}"))
        delete_button = box.addButton(tr("common.delete"), QMessageBox.DestructiveRole)
        box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        self._files.remove(path)
        self._meta.pop(path, None)
        self._refresh()
        self.files_changed.emit()

    def _delete_selected(self) -> None:
        """独立删除按钮：确认后删除全部选中行。"""
        rows = sorted({index.row() for index in self._table.selectionModel().selectedRows()})
        paths = [self._files[r] for r in rows if r < len(self._files)]
        if not paths:
            return
        box = QMessageBox(self)
        box.setWindowTitle(tr("import.confirm_delete_title"))
        box.setText(tr(
            "import.confirm_delete_files",
            count=len(paths),
            files="\n".join(f"· {Path(p).name}" for p in paths),
        ))
        delete_button = box.addButton(tr("common.delete"), QMessageBox.DestructiveRole)
        box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        for path in paths:
            if path in self._files:
                self._files.remove(path)
                self._meta.pop(path, None)
        self._refresh()
        self.files_changed.emit()

    # ------------------------------------------------------------------ 刷新

    def _refresh(self) -> None:
        """文件变化后整体刷新：提示文案、表格与删除按钮显隐。"""
        count = len(self._files)
        self._count_label.setText(tr("import.imported_count_hint", count=count))

        self._table.setRowCount(0)
        for row, path in enumerate(self._files):
            meta = self._meta.get(path, {})
            name = Path(path).name

            index = QTableWidgetItem(str(row + 1))
            index.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name_item = QTableWidgetItem(name)
            name_item.setToolTip(path)
            path_item = QTableWidgetItem(path)
            kind = QTableWidgetItem(meta.get("kind", tr("import.kind.unknown")))
            kind.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            duration = QTableWidgetItem(
                meta.get("duration", tr("import.duration_unknown"))
            )
            duration.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            delete = QPushButton(tr("common.delete"))
            delete.setObjectName("rowDeleteButton")
            delete.setFixedHeight(26)
            delete.clicked.connect(
                lambda _checked=False, p=path: self._delete_one(p)
            )

            self._table.insertRow(row)
            self._table.setItem(row, 0, index)
            self._table.setItem(row, 1, name_item)
            self._table.setItem(row, 2, path_item)
            self._table.setItem(row, 3, kind)
            self._table.setItem(row, 4, duration)
            self._table.setCellWidget(row, 5, delete)

        has_files = count > 0
        self._table.setVisible(has_files)
        self._count_label.setVisible(has_files)
        self._delete_selected_button.setVisible(has_files)
        self._hint_title.setVisible(not has_files)
        self._hint_sub.setVisible(not has_files)
        self._hint_ext.setVisible(not has_files)
        # 有文件时隐藏上下弹簧（表格独占剩余高度）；
        # 无文件时显示（提示文案垂直居中）
        self._top_spacer.setVisible(not has_files)
        self._bottom_spacer.setVisible(not has_files)

    # ------------------------------------------------------------------ 拖拽

    def _is_acceptable(self, path_str: str) -> bool:
        suffix = Path(path_str).suffix.lower()
        return suffix in VIDEO_SUFFIXES or suffix in AUDIO_SUFFIXES

    def _set_hover(self, active: bool) -> None:
        """拖拽悬停时高亮边框（通过动态属性触发 QSS 状态切换）。"""
        self.setProperty("dndHover", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and self._is_acceptable(url.toLocalFile()):
                    self._set_hover(True)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_hover(False)
        paths = [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile() and self._is_acceptable(url.toLocalFile())
        ]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()
            return
        event.ignore()
