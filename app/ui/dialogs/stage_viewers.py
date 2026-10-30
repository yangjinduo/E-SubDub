# -*- coding: utf-8 -*-
"""流程结果查看窗口：任务窗口中点击已完成的流程步骤打开（非模态）。

标题 = 任务显示名（文件名_任务类别）· 流程名，多窗口并存时可区分归属；
每个窗口头部有"打开文件夹"按钮（在资源管理器中定位该流程的结果目录），
右下角为"继续"按钮（从该流程重跑，需确认并覆盖下游产物；任务运行中
禁用），各查看器内容按流程类型定制：
- 提取音频 / 人声分离 / 混音：音轨试听条（可拖进度条）；
- 语音识别 / 文本审核：分段表格（选中行试听该句语音 + 文本）；
- 翻译 / 译文审核：原文译文对照表（选中行试听原声）；
- 配音生成 / 配音复核：每段原文/译文 + 原声/配音试听按钮；
- 封装输出（配音与字幕）：原视频 | 配音后视频（音频输入回退
  dubbed.wav）| 字幕面板，双播放器互斥，字幕高亮跟随正在播放者；
- 封装输出（生成字幕）：原视频 | 字幕面板；
- 文本转语音四流程：分句/情绪表 + 参考音频（可更换）→ 逐句合成结果
  表 → 复核结果表（删除句置灰）→ 成品音频试听 + 字幕面板。

同一任务同一流程的窗口去重：重复点击仅聚焦已有窗口。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSlider,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import tr
from app.core.task_manager import TaskItem, TaskState
from app.core.tts_runner import (
    INTERVAL_SILENCE_SEC,
    TtsTaskConfig,
    normalize_ref_audio,
)
from app.core.workspace import (
    MEDIA_STAGE_ORDER,
    STAGE_ASR,
    STAGE_EXTRACT,
    STAGE_MIX,
    STAGE_MUX,
    STAGE_PROOFREAD,
    STAGE_REVIEW_TRANSLATION,
    STAGE_SEPARATE,
    STAGE_TRANSLATE,
    STAGE_TTS,
    STAGE_TTS_MUX,
    STAGE_TTS_REVIEW,
    STAGE_TTS_SYNTH,
    STAGE_TTS_TEXT,
    STAGE_TTS_VOICE_REVIEW,
    SUBTITLE_STAGE_ORDER,
    TTS_TASK_STAGE_ORDER,
    Workspace,
    downstream_stages,
)
from app.ui.dialogs.tts_edit_dialog import emotion_color, emotion_label
from app.ui.style import table_qss, tokens
from app.ui.widgets.audio_player import AudioPlayerBar
from app.ui.widgets.segments_table import (
    SegmentsTableWidget,
    extract_clip,
    fmt_time,
)
from app.ui.widgets.subtitle_panel import SubtitlePanel

try:  # QVideoWidget 在 QtMultimediaWidgets（PySide6 Addons），精简环境可能缺失
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QVideoWidget

    _QT_VIDEO_OK = True
except ImportError:  # pragma: no cover
    _QT_VIDEO_OK = False


def _row_colors() -> dict:
    """表格行文本 / 背景颜色（按当前主题取令牌，深浅色都清晰）。"""
    c = tokens()
    return {
        "normal": QColor(c["row_text"]),
        "deleted": QColor(c["row_text_deleted"]),
        "bg_normal": QColor(c["row_bg"]),
        "bg_deleted": QColor(c["row_bg_deleted"]),
    }


def _load_stage_segments(ws: Workspace, path: Path) -> list:
    """读取流程分段 JSON；文件缺失或损坏时返回空列表。"""
    try:
        if path.is_file():
            return ws.load_segments(path)
    except (OSError, ValueError):
        pass
    return []


def _segment_audio_source(ws: Workspace) -> str:
    """分段试听音源：优先人声轨，无则用整条音频。"""
    for candidate in (ws.vocals, ws.full_audio):
        if candidate.is_file():
            return str(candidate)
    return ""


def _paint_row(table: QTableWidget, row: int, columns, deleted: bool) -> None:
    """按删除标记刷新整行颜色（置灰 / 恢复）。"""
    colors = _row_colors()
    for col in columns:
        item = table.item(row, col)
        if item is None:
            continue
        item.setForeground(colors["deleted"] if deleted else colors["normal"])
        item.setBackground(colors["bg_deleted"] if deleted else colors["bg_normal"])


# ---------------------------------------------------------------------- 内容控件

class _ClipPlayerMixin:
    """整轨切片播放（ffmpeg 切取片段 + 按起止时间缓存）。"""

    _audio_wav: str
    _clip_cache: dict
    _player: AudioPlayerBar

    def _play_segment_clip(self, seg) -> None:
        if not self._audio_wav or self._player is None:
            return
        start, end = float(seg.start), float(seg.end)
        key = (round(start, 3), round(end, 3))
        clip = self._clip_cache.get(key)
        if clip is None:
            clip = extract_clip(self._audio_wav, start, end) or ""
            self._clip_cache[key] = clip
        if clip:
            self._player.set_source(clip)
            self._player.play()
        else:
            self._player.set_source("")


class _AudioTrackWidget(QFrame):
    """单条音轨：标题 + 试听条（播放按钮 + 可拖进度条）。"""

    def __init__(self, title: str, path: Path | str, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)

        title_label = QLabel(title)
        title_label.setObjectName("viewerTrackTitle")

        self._player = AudioPlayerBar(self)
        self._player.set_source(str(path) if path else "")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(title_label)
        layout.addWidget(self._player)

    def player(self) -> AudioPlayerBar:
        """内部试听条（字幕面板等外部跟随播放位置用）。"""
        return self._player


class _TranslationPairsWidget(QWidget, _ClipPlayerMixin):
    """原文/译文对照表：选中行试听对应说话人的原声。"""

    def __init__(self, segments: list, audio_wav: str, parent=None) -> None:
        super().__init__(parent)
        self._segments = segments
        self._audio_wav = audio_wav or ""
        self._clip_cache: dict = {}

        self._table = QTableWidget(len(self._segments), 5, self)
        self._table.setHorizontalHeaderLabels([
            tr("translation_review.column_index"),
            tr("translation_review.column_time"),
            tr("translation_review.column_speaker"),
            tr("translation_review.column_source"),
            tr("translation_review.column_target"),
        ])
        self._table.setStyleSheet(table_qss())
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
        for col, width in ((0, 46), (1, 130), (2, 70)):
            self._table.setColumnWidth(col, width)

        self._player = AudioPlayerBar(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(self._player)

        self._fill_rows()

    def _fill_rows(self) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        for row, seg in enumerate(self._segments):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, 0, index_item)
            self._table.setItem(row, 1, QTableWidgetItem(
                f"{fmt_time(float(seg.start))} - {fmt_time(float(seg.end))}"
            ))
            self._table.setItem(row, 2, QTableWidgetItem(str(seg.spk)))
            self._table.setItem(row, 3, QTableWidgetItem(str(seg.text)))
            self._table.setItem(row, 4, QTableWidgetItem(str(seg.target_text)))
            _paint_row(self._table, row, (0, 1, 2, 3, 4), bool(seg.deleted))

    def _on_selection_changed(self) -> None:
        row = self._table.currentRow()
        if row < 0 or row >= len(self._segments):
            return
        self._play_segment_clip(self._segments[row])


class _TtsSegmentsWidget(QWidget, _ClipPlayerMixin):
    """配音生成结果表：每段原文/译文 + 原声/配音试听按钮。"""

    def __init__(self, segments: list, audio_wav: str, parent=None) -> None:
        super().__init__(parent)
        self._segments = segments
        self._audio_wav = audio_wav or ""
        self._clip_cache: dict = {}

        self._table = QTableWidget(len(self._segments), 7, self)
        self._table.setHorizontalHeaderLabels([
            tr("translation_review.column_index"),
            tr("translation_review.column_time"),
            tr("translation_review.column_speaker"),
            tr("translation_review.column_source"),
            tr("translation_review.column_target"),
            tr("viewer.tts.original"),
            tr("viewer.tts.dubbed"),
        ])
        self._table.setStyleSheet(table_qss())
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(42)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 46), (1, 130), (2, 70), (5, 64), (6, 64)):
            self._table.setColumnWidth(col, width)

        self._player = AudioPlayerBar(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._table, stretch=1)
        layout.addWidget(self._player)

        self._fill_rows()

    def _fill_rows(self) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        for row, seg in enumerate(self._segments):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, 0, index_item)
            self._table.setItem(row, 1, QTableWidgetItem(
                f"{fmt_time(float(seg.start))} - {fmt_time(float(seg.end))}"
            ))
            self._table.setItem(row, 2, QTableWidgetItem(str(seg.spk)))
            self._table.setItem(row, 3, QTableWidgetItem(str(seg.text)))
            self._table.setItem(row, 4, QTableWidgetItem(str(seg.target_text)))
            _paint_row(self._table, row, (0, 1, 2, 3, 4), bool(seg.deleted))

            self._table.setCellWidget(
                row, 5, self._make_button(row, original=True)
            )
            self._table.setCellWidget(
                row, 6, self._make_button(row, original=False)
            )

    def _make_button(self, row: int, original: bool) -> QWidget:
        """构造"原声 / 配音"试听按钮（音源不可用时禁用）。"""
        seg = self._segments[row]
        if original:
            enabled = bool(self._audio_wav)
        else:
            path = str(seg.tts_audio_path or "")
            enabled = bool(path) and os.path.isfile(path)

        button = QPushButton(tr("viewer.tts.original" if original else "viewer.tts.dubbed"))
        button.setObjectName("cardSettingButton")
        button.setEnabled(enabled)
        button.clicked.connect(
            lambda _=False, r=row, o=original: self._on_listen(r, o)
        )

        holder = QWidget()
        holder_layout = QHBoxLayout(holder)
        holder_layout.setContentsMargins(6, 2, 6, 2)
        holder_layout.addWidget(button)
        return holder

    def _on_listen(self, row: int, original: bool) -> None:
        seg = self._segments[row]
        if original:
            self._play_segment_clip(seg)
            return
        path = str(seg.tts_audio_path or "")
        if path and os.path.isfile(path):
            self._player.set_source(path)
            self._player.play()


def _fmt_pos(ms: int) -> str:
    """毫秒 → "分:秒"。"""
    seconds = max(0, ms // 1000)
    return f"{seconds // 60}:{seconds % 60:02d}"


class _VideoPlayerWidget(QFrame):
    """单个视频播放位：标题 + 画面/回退提示 + 播放控制条。

    播放组件不可用、文件缺失或解码出错时显示回退提示
    （用户仍可通过窗口头部"打开文件夹"查看文件）。
    """

    playback_started = Signal()  # 开始播放（多播放器互斥用）

    def __init__(self, title: str, path: str, parent=None) -> None:
        super().__init__(parent)
        self.setProperty("card", True)
        self._path = str(path)
        self._playing = False
        self._player: QMediaPlayer | None = None
        self._video_widget: QVideoWidget | None = None

        title_label = QLabel(title)
        title_label.setObjectName("viewerTrackTitle")

        self._fallback_label = QLabel("")
        self._fallback_label.setObjectName("viewerFallback")
        self._fallback_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fallback_label.setWordWrap(True)
        self._fallback_label.hide()

        # 控制条控件（先创建：_show_fallback 回退路径会访问它们）
        self._play_button = QPushButton(tr("player.play"))
        self._play_button.setFixedWidth(72)
        self._play_button.clicked.connect(self._on_button_clicked)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._on_slider_moved)

        self._time_label = QLabel("")
        self._time_label.setStyleSheet("color: #6B7280; font-size: 12px;")

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_tick)

        # 播放后端初始化（不可用时回退为提示）
        if _QT_VIDEO_OK and os.path.isfile(self._path):
            try:
                self._video_widget = QVideoWidget()
                self._video_widget.setMinimumHeight(240)
                self._player = QMediaPlayer(self)
                audio_out = QAudioOutput(self)
                self._player.setAudioOutput(audio_out)
                self._player.setVideoOutput(self._video_widget)
                self._player.setSource(QUrl.fromLocalFile(self._path))
                self._player.errorOccurred.connect(self._on_player_error)
            except Exception:
                self._video_widget = None
                self._player = None
        if self._player is None:
            self._show_fallback()
        else:
            self._time_label.setText("0:00 / 0:00")

        controls = QHBoxLayout()
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(10)
        controls.addWidget(self._play_button)
        controls.addWidget(self._slider, stretch=1)
        controls.addWidget(self._time_label)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        layout.addWidget(title_label)
        if self._video_widget is not None:
            layout.addWidget(self._video_widget, 1)
        layout.addWidget(self._fallback_label, 1)
        layout.addLayout(controls)

    # -------------------------------------------------------------- 公开接口

    def is_playing(self) -> bool:
        """是否正在播放。"""
        return self._playing

    def position(self) -> int:
        """当前播放位置（毫秒）；播放组件不可用时返回 0。"""
        if self._player is not None:
            try:
                return int(self._player.position())
            except Exception:
                return 0
        return 0

    def pause(self) -> None:
        """暂停播放（保持进度；另一播放器开始播放时互斥调用）。"""
        if self._player is None or not self._playing:
            return
        try:
            self._player.pause()
        except Exception:
            return
        self._timer.stop()
        self._playing = False
        self._play_button.setText(tr("player.play"))

    # -------------------------------------------------------------- 内部槽

    def _show_fallback(self, error: str = "") -> None:
        """隐藏画面，显示回退提示。"""
        if self._video_widget is not None:
            self._video_widget.hide()
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        self._timer.stop()
        self._playing = False
        self._play_button.setEnabled(False)
        self._slider.setEnabled(False)
        if error:
            self._fallback_label.setText(tr("viewer.video.play_error", error=error))
        else:
            self._fallback_label.setText(tr("viewer.video.unavailable", path=self._path))
        self._fallback_label.show()

    def _on_player_error(self, _err, message: str) -> None:
        self._show_fallback(str(message))

    def _on_button_clicked(self) -> None:
        if self._playing:
            self._stop()
        else:
            self._play()

    def _play(self) -> None:
        if self._player is None:
            return
        try:
            self._player.play()
            self._playing = True
            self._play_button.setText(tr("player.stop"))
            self._timer.start()
            self.playback_started.emit()
        except Exception:
            self._show_fallback()

    def _stop(self) -> None:
        if self._player is None:
            return
        try:
            self._player.stop()
        except Exception:
            pass
        self._timer.stop()
        self._playing = False
        self._play_button.setText(tr("player.play"))
        self._slider.setValue(0)

    def _on_slider_moved(self, value: int) -> None:
        if self._player is not None:
            self._player.setPosition(value)

    def _on_tick(self) -> None:
        if self._player is None:
            return
        position = self._player.position()
        duration = self._player.duration()
        if duration > 0:
            self._slider.setRange(0, duration)
        self._slider.setValue(position)
        self._time_label.setText(f"{_fmt_pos(position)} / {_fmt_pos(duration)}")


# ---------------------------------------------------------------------- 查看窗口

class StageViewerWindow(QDialog):
    """流程结果查看窗口基类：标题 + 目录路径 + 打开文件夹按钮 + 内容区。

    子类实现 _build_content() 向内容布局添加控件。接入 TaskManager
    （manager 非空）时右下角为"继续"按钮：确认后重置当前流程及全部
    下游 manifest 标记并重新入列（产物覆盖重跑），同时关闭该任务已
    打开的全部查看窗口；任务排队/运行/取消中时按钮禁用。未接入
    manager 时退回"关闭"按钮（兼容旧调用方）。
    """

    def __init__(
        self, item: TaskItem, stage_id: str, parent=None, manager=None,
    ) -> None:
        super().__init__(parent)
        self._item = item
        self._stage_id = stage_id
        self._manager = manager
        self._ws: Workspace = item.workspace
        self.setWindowTitle(f"{item.display_name} · {tr(f'step.{stage_id}')}")
        self.setWindowFlags(Qt.WindowType.Window)  # 完整窗口装饰（最小化/最大化）
        # 关闭即销毁（QDialog 默认仅隐藏），确保播放器随之释放
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(920, 640)

        root = QVBoxLayout(self)
        root.setContentsMargins(20, 16, 20, 16)
        root.setSpacing(12)

        header = QHBoxLayout()
        self._path_label = QLabel(str(self._folder_to_open()))
        self._path_label.setObjectName("viewerPathLabel")
        self._path_label.setWordWrap(True)
        open_button = QPushButton(tr("viewer.open_folder"))
        open_button.setObjectName("cardSettingButton")
        open_button.clicked.connect(self._open_folder)
        header.addWidget(self._path_label, 1)
        header.addWidget(open_button)
        root.addLayout(header)

        self._content = QVBoxLayout()
        self._content.setContentsMargins(0, 0, 0, 0)
        self._content.setSpacing(12)
        root.addLayout(self._content, 1)

        bottom_row = QHBoxLayout()
        bottom_row.addStretch(1)
        if manager is not None:
            self._continue_button = QPushButton(tr("viewer.continue"))
            self._continue_button.setObjectName("primaryButton")
            self._continue_button.clicked.connect(self._on_continue)
            bottom_row.addWidget(self._continue_button)
            manager.task_state_changed.connect(self._on_manager_state_changed)
            self._update_continue_state()
        else:
            self._continue_button = None
            close_button = QPushButton(tr("common.close"))
            close_button.clicked.connect(self.close)
            bottom_row.addWidget(close_button)
        root.addLayout(bottom_row)

        self._build_content()

    def closeEvent(self, event) -> None:
        """关闭窗口时停止所有仍在播放的音频/视频，避免后台继续出声。"""
        for player in self.findChildren(AudioPlayerBar):
            player.stop()
        if _QT_VIDEO_OK:
            for player in self.findChildren(QMediaPlayer):
                try:
                    player.stop()
                except Exception:
                    pass
        super().closeEvent(event)

    # -------------------------------------------------------------- 继续执行

    def _on_manager_state_changed(self, task_id: str) -> None:
        if task_id == self._item.id:
            self._update_continue_state()

    def _update_continue_state(self) -> None:
        """任务处于排队/运行/取消中时禁用"继续"，终态时启用。"""
        if self._manager is None or self._continue_button is None:
            return
        item = self._manager.get_task(self._item.id)
        state = item.state if item is not None else TaskState.DONE
        self._continue_button.setEnabled(TaskState.is_terminal(state))

    def _stage_order(self) -> tuple[str, ...]:
        """本任务的流程顺序表（决定重跑时重置哪些下游阶段）。"""
        cfg = self._item.config
        if isinstance(cfg, TtsTaskConfig):
            return TTS_TASK_STAGE_ORDER
        if getattr(cfg, "dubbing_enabled", False):
            return MEDIA_STAGE_ORDER
        return SUBTITLE_STAGE_ORDER

    def _on_continue(self) -> None:
        """从当前查看的流程重跑：确认后重置该阶段及全部下游并重新入列。"""
        stages = downstream_stages(self._stage_id, self._stage_order())
        if not stages:
            return
        answer = QMessageBox.question(
            self,
            tr("viewer.continue_title"),
            tr("viewer.continue_confirm", stage=tr(f"step.{self._stage_id}")),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._item.workspace.reset_stages(stages)
        close_all_viewers(self._item.id)  # 含自身：重跑后产物已失效
        self._manager.retry(self._item.id)

    # -------------------------------------------------------------- 子类接口

    def _build_content(self) -> None:
        """构建内容区（子类覆写）。"""
        self._add_hint(tr("viewer.no_data"))

    # -------------------------------------------------------------- 工具

    def _add_hint(self, text: str) -> None:
        label = QLabel(text)
        label.setObjectName("viewerHint")
        label.setWordWrap(True)
        self._content.addWidget(label, 0, Qt.AlignmentFlag.AlignTop)

    def _folder_to_open(self) -> Path:
        """查看器对应的文件夹（流程子夹；mux 等无子夹时为工作夹根目录）。"""
        folder = self._ws.stage_dir(self._stage_id)
        return folder if folder.is_dir() else self._ws.dir

    def _open_folder(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self._folder_to_open())))


class _AudioTracksViewer(StageViewerWindow):
    """纯音轨查看基类：若干条音轨纵向排列。"""

    _track_defs: list[tuple[str, object]] = []  # (标题 key, Workspace 属性名)

    def _build_content(self) -> None:
        for key, attr in self._track_defs:
            self._content.addWidget(
                _AudioTrackWidget(tr(key), getattr(self._ws, attr))
            )
        self._content.addStretch(1)
        self.resize(780, 220 + 110 * len(self._track_defs))


class ExtractAudioViewer(_AudioTracksViewer):
    """提取音频：播放已提取的音频。"""

    _track_defs = [("viewer.track.extracted", "full_audio")]


class SeparateViewer(_AudioTracksViewer):
    """人声分离：分离前 / 人声 / 背景音三条音轨。"""

    _track_defs = [
        ("viewer.track.original_full", "full_audio"),
        ("viewer.track.vocals", "vocals"),
        ("viewer.track.background", "no_vocals"),
    ]


class MixViewer(_AudioTracksViewer):
    """混音：原音频与混音后音频。"""

    _track_defs = [
        ("viewer.track.original", "full_audio"),
        ("viewer.track.mixed", "mixed_audio"),
    ]


class _SegmentsStageViewer(StageViewerWindow):
    """分段文本类流程查看基类：只读分段表格 + 选中行试听。"""

    def _segments_path(self) -> Path:
        raise NotImplementedError

    def _build_content(self) -> None:
        segments = _load_stage_segments(self._ws, self._segments_path())
        if not segments:
            self._add_hint(tr("viewer.no_data"))
            return
        audio = _segment_audio_source(self._ws)
        self._content.addWidget(
            SegmentsTableWidget(segments, audio, readonly=True, parent=self)
        )


class AsrViewer(_SegmentsStageViewer):
    """语音识别：识别文本 + 分段语音试听。"""

    def _segments_path(self) -> Path:
        return self._ws.asr_json


class ProofreadViewer(_SegmentsStageViewer):
    """文本审核：人工审核定稿文本 + 分段语音试听。"""

    def _segments_path(self) -> Path:
        return self._ws.reviewed_json


class _TranslationStageViewer(StageViewerWindow):
    """翻译类流程查看基类：原文/译文对照表 + 选中行试听原声。"""

    def _segments_path(self) -> Path:
        raise NotImplementedError

    def _build_content(self) -> None:
        segments = _load_stage_segments(self._ws, self._segments_path())
        if not segments:
            self._add_hint(tr("viewer.no_data"))
            return
        audio = _segment_audio_source(self._ws)
        self._content.addWidget(_TranslationPairsWidget(segments, audio, self))


class TranslateViewer(_TranslationStageViewer):
    """翻译：原文与大模型译文对照。"""

    def _segments_path(self) -> Path:
        return self._ws.translated_pre


class TranslationReviewViewer(_TranslationStageViewer):
    """译文审核：原文与审核后译文对照。"""

    def _segments_path(self) -> Path:
        return self._ws.translated_post


class TtsViewer(StageViewerWindow):
    """配音生成：每段原声/译文/新生成的配音。"""

    def _segments_path(self) -> Path:
        return self._ws.tts_json

    def _build_content(self) -> None:
        segments = _load_stage_segments(self._ws, self._segments_path())
        if not segments:
            self._add_hint(tr("viewer.no_data"))
            return
        audio = _segment_audio_source(self._ws)
        self._content.addWidget(_TtsSegmentsWidget(segments, audio, self))


class TtsReviewViewer(TtsViewer):
    """配音复核：复核后的配音结果（译文/配音文件的最终形态）。"""

    def _segments_path(self) -> Path:
        return self._ws.tts_reviewed_json


class MuxViewer(StageViewerWindow):
    """封装输出（配音与字幕 / 生成字幕）：按任务形态分流显示。

    配音与字幕任务：原视频 | 配音后视频（MKV；音频输入无视频流时回退
    dubbed.wav）| 字幕面板；生成字幕任务：原视频 | 字幕面板（无配音位）。
    两个视频播放器互斥（播放其一自动暂停另一个，都停止时保持最后
    位置），字幕高亮跟随正在播放的播放器。
    """

    def _build_content(self) -> None:
        cfg = self._item.config
        self.resize(1280, 720)
        original = _VideoPlayerWidget(
            tr("viewer.video.original"), str(cfg.media_path), self,
        )
        panel = SubtitlePanel(self)
        panel.load_latest_srt_from(self._ws.stage_dir(STAGE_MUX))

        row = QHBoxLayout()
        row.setSpacing(12)
        if getattr(cfg, "dubbing_enabled", False):
            dubbed_path = str(self._ws.output_mkv)
            if not Path(dubbed_path).is_file():
                dubbed_path = str(self._ws.dubbed_wav)  # 音频输入成品回退
            dubbed = _VideoPlayerWidget(
                tr("viewer.video.dubbed"), dubbed_path, self,
            )
            original.playback_started.connect(dubbed.pause)
            dubbed.playback_started.connect(original.pause)
            panel.set_players([original, dubbed])
            row.addWidget(original, 1)
            row.addWidget(dubbed, 1)
        else:
            panel.set_players([original])
            row.addWidget(original, 1)
        row.addWidget(panel, 1)
        self._content.addLayout(row, 1)


# ---------------------------------------------------------------------- 文本转语音

def _read_stage_json(path: Path) -> dict | None:
    """读取流程 JSON；文件缺失或损坏时返回 None。"""
    try:
        if path.is_file():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, ValueError):
        pass
    return None


def _fmt_secs(seconds: float) -> str:
    """秒 → "分:秒.十分位"（与复核对话框一致）。"""
    if seconds < 0:
        seconds = 0.0
    minutes = int(seconds // 60)
    return f"{minutes:02d}:{seconds - minutes * 60:04.1f}"


class TtsTextViewer(StageViewerWindow):
    """文本处理（文本转语音）：分句/情绪表 + 参考音频试听（可更换）。

    更换参考音频：规范化覆盖 01_文本处理/ref_audio.wav 并回写任务配置
    的源路径（输入指纹随源路径变化，下次执行四阶段全量重跑）。
    """

    def _build_content(self) -> None:
        data = _read_stage_json(self._ws.tts_sentences_json) or {}
        sentences = data.get("sentences") or []
        if not sentences:
            self._add_hint(tr("viewer.no_data"))
            return

        lang = str(data.get("lang") or "")
        if lang:
            from app.core.langid_util import language_display_name

            lang_label = QLabel(
                tr("viewer.tts.lang", lang=language_display_name(lang))
            )
            lang_label.setStyleSheet("color: #6B7280;")
            self._content.addWidget(lang_label)

        table = QTableWidget(len(sentences), 3, self)
        table.setHorizontalHeaderLabels([
            tr("tts_voice_review.column_index"),
            tr("tts_voice_review.column_content"),
            tr("tts_voice_review.column_emotion"),
        ])
        table.setStyleSheet(table_qss())
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(42)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setWordWrap(True)
        header = table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setColumnWidth(0, 46)
        table.setColumnWidth(2, 110)
        center = Qt.AlignmentFlag.AlignCenter
        for row, sent in enumerate(sentences):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            table.setItem(row, 0, index_item)
            table.setItem(row, 1, QTableWidgetItem(str(sent.get("text") or "")))
            emo_key = sent.get("emo")
            emo_item = QTableWidgetItem(emotion_label(emo_key))
            emo_item.setTextAlignment(center)
            emo_item.setForeground(QColor(emotion_color(emo_key)))
            table.setItem(row, 2, emo_item)
        self._content.addWidget(table, 1)

        ref_row = QHBoxLayout()
        ref_row.setSpacing(10)
        self._ref_player = AudioPlayerBar(self)
        self._ref_player.set_source(str(self._ws.tts_ref_audio))
        change_button = QPushButton(tr("viewer.tts.change_ref"))
        change_button.setObjectName("cardSettingButton")
        change_button.clicked.connect(self._on_change_ref)
        ref_row.addWidget(self._ref_player, 1)
        ref_row.addWidget(change_button)
        self._content.addLayout(ref_row)

    def _on_change_ref(self) -> None:
        """更换参考音频：规范化覆盖产物并回写配置源路径。"""
        path, _filter = QFileDialog.getOpenFileName(
            self, tr("viewer.tts.change_ref_title"), "",
            "Audio (*.wav *.mp3 *.m4a *.flac *.aac *.ogg *.opus)",
        )
        if not path:
            return
        self._ref_player.release()  # 先释放文件句柄，避免覆盖被占用
        try:
            normalize_ref_audio(Path(path), self._ws.tts_ref_audio)
        except Exception as exc:  # noqa: BLE001 - 规范化失败提示后保持原状
            QMessageBox.warning(
                self, tr("viewer.tts.change_ref"), str(exc)
            )
            self._ref_player.set_source(str(self._ws.tts_ref_audio))
            return
        cfg = self._item.config
        if isinstance(cfg, TtsTaskConfig):
            cfg.ref_audio_path = path  # 指纹含源路径 → 下次执行全量重跑
        self._ref_player.set_source(str(self._ws.tts_ref_audio))
        QMessageBox.information(
            self, tr("viewer.tts.change_ref"), tr("viewer.tts.ref_changed")
        )


class _TtsSentencesWidget(QWidget):
    """文本转语音逐句结果表：序号 / 时间 / 内容 / 情绪 / 试听。

    rows = [{text, emo, file, duration, deleted}]（file 为工作夹相对路径）；
    时间列按成品时间轴顺序累计（删除句显示 "-"，不占位）；删除句整行
    置灰；试听直接播放该句 WAV。
    """

    def __init__(self, rows: list, ws_dir: str, parent=None) -> None:
        super().__init__(parent)
        self._rows = rows
        self._ws_dir = ws_dir

        self._table = QTableWidget(len(rows), 5, self)
        self._table.setHorizontalHeaderLabels([
            tr("tts_voice_review.column_index"),
            tr("tts_voice_review.column_time"),
            tr("tts_voice_review.column_content"),
            tr("tts_voice_review.column_emotion"),
            tr("tts_voice_review.column_listen"),
        ])
        self._table.setStyleSheet(table_qss())
        self._table.verticalHeader().setVisible(False)
        self._table.verticalHeader().setDefaultSectionSize(42)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setWordWrap(True)
        header = self._table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for col, width in ((0, 46), (1, 130), (3, 90), (4, 70)):
            self._table.setColumnWidth(col, width)

        self._player = AudioPlayerBar(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self._table, 1)
        layout.addWidget(self._player)

        self._fill_rows()

    def _fill_rows(self) -> None:
        center = Qt.AlignmentFlag.AlignCenter
        spans = self._timeline()
        for row, entry in enumerate(self._rows):
            index_item = QTableWidgetItem(str(row + 1))
            index_item.setTextAlignment(center)
            self._table.setItem(row, 0, index_item)
            span = spans[row]
            self._table.setItem(row, 1, QTableWidgetItem(
                f"{_fmt_secs(span[0])} - {_fmt_secs(span[1])}" if span else "-"
            ))
            self._table.setItem(row, 2, QTableWidgetItem(
                str(entry.get("text") or "")
            ))
            emo_key = entry.get("emo")
            emo_item = QTableWidgetItem(emotion_label(emo_key))
            emo_item.setTextAlignment(center)
            emo_item.setForeground(QColor(emotion_color(emo_key)))
            self._table.setItem(row, 3, emo_item)

            listen_button = QPushButton(tr("tts_voice_review.listen"))
            listen_button.setObjectName("cardSettingButton")
            rel = str(entry.get("file") or "")
            path = os.path.join(self._ws_dir, rel) if rel and self._ws_dir else rel
            listen_button.setEnabled(bool(path) and os.path.isfile(path))
            listen_button.clicked.connect(
                lambda _=False, p=path: self._on_listen(p)
            )
            holder = QWidget()
            holder_layout = QHBoxLayout(holder)
            holder_layout.setContentsMargins(6, 2, 6, 2)
            holder_layout.addWidget(listen_button)
            self._table.setCellWidget(row, 4, holder)

            _paint_row(self._table, row, (0, 1, 2, 3), bool(entry.get("deleted")))

    def _timeline(self) -> "list[tuple[float, float] | None]":
        """成品时间轴：保留句实际时长顺序累计（删除句不占位，记 None）。"""
        spans: "list[tuple[float, float] | None]" = []
        cursor = 0.0
        for entry in self._rows:
            if entry.get("deleted"):
                spans.append(None)
                continue
            duration = float(entry.get("duration") or 0.0)
            spans.append((cursor, cursor + duration))
            cursor += duration + INTERVAL_SILENCE_SEC
        return spans

    def _on_listen(self, path: str) -> None:
        if path and os.path.isfile(path):
            self._player.set_source(path)
            self._player.play()


class _TtsSentencesStageViewer(StageViewerWindow):
    """逐句结果类流程查看基类（语音合成 / 语音复核）。"""

    def _rows(self) -> list | None:
        raise NotImplementedError

    def _build_content(self) -> None:
        rows = self._rows()
        if not rows:
            self._add_hint(tr("viewer.no_data"))
            return
        self._content.addWidget(
            _TtsSentencesWidget(rows, str(self._ws.dir), self)
        )


class TtsSynthViewer(_TtsSentencesStageViewer):
    """语音合成（文本转语音）：逐句合成结果（合成记录 + 分句情绪）。"""

    def _rows(self) -> list | None:
        sentences = (
            _read_stage_json(self._ws.tts_sentences_json) or {}
        ).get("sentences") or []
        entries = (
            _read_stage_json(self._ws.tts_synth_json) or {}
        ).get("entries") or []
        if not sentences or len(sentences) != len(entries):
            return None
        return [
            {
                "text": sent.get("text"),
                "emo": sent.get("emo"),
                "file": entry.get("file"),
                "duration": entry.get("duration"),
            }
            for sent, entry in zip(sentences, entries)
        ]


class TtsVoiceReviewViewer(_TtsSentencesStageViewer):
    """语音复核（文本转语音）：复核结果（删除标记 + 重生成后时长）。"""

    def _rows(self) -> list | None:
        return (
            _read_stage_json(self._ws.tts_voice_reviewed_json) or {}
        ).get("entries") or None


class TtsMuxViewer(StageViewerWindow):
    """封装输出（文本转语音）：成品音频试听 + 字幕面板（生成 SRT 时）。"""

    def _build_content(self) -> None:
        self.resize(980, 640)
        rel = str(self._ws.stage_flag("tts_final_audio") or "")
        audio_path = str(self._ws.dir / rel) if rel else ""
        if not Path(audio_path).is_file():
            # 标记缺失兜底：封装夹内任意音频成品（扩展名随输出格式变化）
            mux_dir = self._ws.stage_dir(STAGE_TTS_MUX)
            audio_path = ""
            if mux_dir.is_dir():
                for candidate in sorted(mux_dir.iterdir()):
                    if candidate.suffix.lower() in {
                        ".wav", ".mp3", ".m4a", ".flac",
                    }:
                        audio_path = str(candidate)
                        break

        audio_widget = _AudioTrackWidget(
            tr("viewer.tts.final_audio"), audio_path, self,
        )
        self._content.addWidget(audio_widget)

        panel = SubtitlePanel(self)
        if panel.load_latest_srt_from(self._ws.stage_dir(STAGE_TTS_MUX)):
            panel.set_players([audio_widget.player()])
        self._content.addWidget(panel, 1)


# ---------------------------------------------------------------------- 工厂

_VIEWER_CLASSES: dict[str, type[StageViewerWindow]] = {
    STAGE_EXTRACT: ExtractAudioViewer,
    STAGE_SEPARATE: SeparateViewer,
    STAGE_ASR: AsrViewer,
    STAGE_PROOFREAD: ProofreadViewer,
    STAGE_TRANSLATE: TranslateViewer,
    STAGE_REVIEW_TRANSLATION: TranslationReviewViewer,
    STAGE_TTS: TtsViewer,
    STAGE_TTS_REVIEW: TtsReviewViewer,
    STAGE_MIX: MixViewer,
    STAGE_MUX: MuxViewer,
    STAGE_TTS_TEXT: TtsTextViewer,
    STAGE_TTS_SYNTH: TtsSynthViewer,
    STAGE_TTS_VOICE_REVIEW: TtsVoiceReviewViewer,
    STAGE_TTS_MUX: TtsMuxViewer,
}

# 已打开的查看窗口（(task_id, stage_id) → 窗口），销毁时自动移除
_OPEN_VIEWERS: dict[tuple[str, str], StageViewerWindow] = {}


def open_stage_viewer(
    item: TaskItem, stage_id: str, parent=None, manager=None,
) -> StageViewerWindow:
    """打开（或聚焦已打开的）某任务某流程的结果查看窗口（非模态）。

    manager 非空时窗口右下角提供"继续"按钮（从该流程重跑）。
    """
    key = (item.id, stage_id)
    existing = _OPEN_VIEWERS.get(key)
    if existing is not None:
        existing.show()
        existing.raise_()
        existing.activateWindow()
        return existing

    viewer_cls = _VIEWER_CLASSES.get(stage_id, StageViewerWindow)
    window = viewer_cls(item, stage_id, parent, manager=manager)
    _OPEN_VIEWERS[key] = window
    window.destroyed.connect(lambda *_args, k=key: _OPEN_VIEWERS.pop(k, None))
    window.show()
    return window


def close_all_viewers(task_id: str) -> None:
    """关闭某任务已打开的全部查看窗口（继续执行确认后调用）。"""
    for (tid, _stage), window in list(_OPEN_VIEWERS.items()):
        if tid == task_id:
            window.close()
