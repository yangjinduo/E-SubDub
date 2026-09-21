"""中部导入区：支持拖拽文件或点击选择，导入后显示文件名、时长、类型。"""

import re
import subprocess
import sys
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFileDialog, QFrame, QLabel, QVBoxLayout

from app.core.ffmpeg import get_ffmpeg_path
from app.core.i18n import tr

# 支持的文件后缀 → 类型显示名
VIDEO_SUFFIXES = {".mkv": "MKV", ".mp4": "MP4", ".mov": "MOV", ".avi": "AVI"}
AUDIO_SUFFIXES = {".wav": "WAV", ".mp3": "MP3", ".flac": "FLAC", ".m4a": "M4A"}


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

# ffmpeg -i 输出中的时长行，如 "  Duration: 00:01:23.45, start: ..."
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


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


class ImportArea(QFrame):
    """大区域导入控件：拖拽或点击导入媒体文件。"""

    # 成功导入文件时发射，参数为文件绝对路径
    file_imported = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("importDropArea")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(180)

        self._current_file: str | None = None

        # 引导文案
        self._hint_title = QLabel(tr("import.hint_title"))
        self._hint_title.setObjectName("importHintTitle")
        self._hint_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_sub = QLabel(tr("import.hint_sub"))
        self._hint_sub.setObjectName("importHintSub")
        self._hint_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_ext = QLabel(tr("import.hint_ext"))
        self._hint_ext.setObjectName("importHintSub")
        self._hint_ext.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 导入后的文件信息卡片（初始隐藏）
        self._file_card = QFrame()
        self._file_card.setObjectName("fileInfoCard")
        self._file_card.setFixedWidth(520)
        card_layout = QVBoxLayout(self._file_card)
        card_layout.setContentsMargins(16, 12, 16, 12)
        card_layout.setSpacing(4)
        self._file_name_label = QLabel("文件名")
        self._file_name_label.setObjectName("fileInfoName")
        self._file_path_label = QLabel("路径")
        self._file_path_label.setObjectName("fileInfoPath")
        self._file_path_label.setWordWrap(True)
        self._file_meta_label = QLabel("类型 / 时长")
        self._file_meta_label.setObjectName("fileInfoMeta")
        card_layout.addWidget(self._file_name_label)
        card_layout.addWidget(self._file_path_label)
        card_layout.addWidget(self._file_meta_label)
        self._file_card.hide()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(8)
        layout.addStretch(1)
        layout.addWidget(self._hint_title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._hint_sub, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._hint_ext, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addSpacing(8)
        layout.addWidget(self._file_card, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addStretch(1)

    # ------------------------------------------------------------------ 属性

    @property
    def current_file(self) -> str | None:
        """当前已导入的文件路径（未导入为 None）。"""
        return self._current_file

    # ------------------------------------------------------------------ 交互

    def mousePressEvent(self, event) -> None:
        """点击空白区域弹出文件选择框。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self._choose_file()
        super().mousePressEvent(event)

    def _choose_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, tr("import.choose_title"), str(Path.home()), _file_filter()
        )
        if path:
            self.set_file(path)

    def set_file(self, path: str) -> None:
        """设置当前文件并刷新展示（时长通过 ffmpeg 探测）。"""
        self._current_file = path
        self._file_name_label.setText(Path(path).name)
        self._file_path_label.setText(path)
        kind = _file_kind_label(path)
        duration = _probe_duration(path)
        self._file_meta_label.setText(tr("import.meta", kind=kind, duration=duration))
        self._file_card.show()
        # 提示文案切换为"已导入"状态
        self._hint_title.setText(tr("import.imported_hint"))
        self.file_imported.emit(path)

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
        for url in event.mimeData().urls():
            if url.isLocalFile() and self._is_acceptable(url.toLocalFile()):
                self.set_file(url.toLocalFile())
                event.acceptProposedAction()
                return
        event.ignore()
