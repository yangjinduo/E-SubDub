# -*- coding: utf-8 -*-
"""共用音频试听控件：播放/停止按钮 + 进度滑块 + 时间标签。

后端优先使用 QtMultimedia（QMediaPlayer + QAudioOutput，PySide6 自带）；
在其不可用或初始化失败时，Windows 下降级为 winsound 异步播放（无进度反馈）。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer, QUrl, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QWidget

from app.core.i18n import tr

try:  # QtMultimedia 随 PySide6 提供，个别精简环境可能缺失
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    _QT_MULTIMEDIA_OK = True
except ImportError:  # pragma: no cover
    _QT_MULTIMEDIA_OK = False


def _fmt_ms(ms: int) -> str:
    """毫秒 → "分:秒.十分位"（如 8340 → "0:08.3"）。"""
    if ms < 0:
        ms = 0
    seconds, remainder = divmod(ms, 1000)
    return f"{seconds // 60}:{seconds % 60:02d}.{remainder // 100}"


class AudioPlayerBar(QWidget):
    """紧凑音频试听条。

    - set_source(path)：加载音频文件；路径为空或文件不存在时整体禁用
    - play()/stop()：公开方法，可供外部触发与测试直接调用
    - 进度滑块为简单实现：QTimer 每 100ms 轮询 QMediaPlayer.position
    """

    finished = Signal()  # 一次播放自然结束（EndOfMedia）时发射

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._source = ""
        self._playing = False
        self._player: "QMediaPlayer | None" = None
        self._audio_out: "QAudioOutput | None" = None
        self._use_winsound = False  # QtMultimedia 不可用且在 Windows 时的降级开关

        self._play_button = QPushButton(tr("player.play"))
        self._play_button.setFixedWidth(72)
        self._play_button.clicked.connect(self._on_button_clicked)

        self._slider = QSlider(Qt.Orientation.Horizontal)
        self._slider.setRange(0, 0)
        self._slider.sliderMoved.connect(self._on_slider_moved)

        self._time_label = QLabel(tr("player.no_audio"))
        self._time_label.setStyleSheet("color: #6B7280; font-size: 12px;")
        self._time_label.setMinimumWidth(110)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(self._play_button)
        layout.addWidget(self._slider, stretch=1)
        layout.addWidget(self._time_label)

        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_tick)

        self._init_backend()
        self.set_source("")

    # -------------------------------------------------------------- 后端初始化

    def _init_backend(self) -> None:
        """初始化播放后端：优先 QMediaPlayer，异常时降级 winsound。"""
        if _QT_MULTIMEDIA_OK:
            try:
                self._player = QMediaPlayer(self)
                self._audio_out = QAudioOutput(self)
                self._player.setAudioOutput(self._audio_out)
                self._player.mediaStatusChanged.connect(self._on_media_status)
                self._player.errorOccurred.connect(self._on_player_error)
                return
            except Exception:
                self._player = None
                self._audio_out = None
        # 降级：Windows winsound 异步播放（无进度/时长反馈）
        self._use_winsound = os.name == "nt"

    # -------------------------------------------------------------- 公开接口

    def set_source(self, path: str) -> None:
        """加载音频文件；空路径或文件不存在时禁用整个控件。"""
        self.stop()
        self._source = str(path) if path else ""
        valid = bool(self._source) and os.path.isfile(self._source)
        if valid and self._player is not None:
            self._player.setSource(QUrl.fromLocalFile(self._source))
        self._play_button.setEnabled(valid)
        self._slider.setEnabled(valid and self._player is not None)
        self._slider.setRange(0, 0)
        if valid:
            self._time_label.setText("0:00.0")
        else:
            self._time_label.setText(
                tr("player.no_audio") if not self._source else tr("player.file_missing")
            )

    def source(self) -> str:
        """返回当前加载的音频路径（可能为空串）。"""
        return self._source

    def play(self) -> None:
        """开始播放；无有效音源时直接返回，不抛异常。"""
        if not self._source or not os.path.isfile(self._source):
            return
        if self._player is not None:
            try:
                self._player.play()
                self._playing = True
                self._play_button.setText(tr("player.stop"))
                self._timer.start()
            except Exception:
                self._reset_playing_ui()
        elif self._use_winsound:
            import winsound

            try:
                winsound.PlaySound(
                    self._source, winsound.SND_FILENAME | winsound.SND_ASYNC
                )
                self._playing = True
                self._play_button.setText(tr("player.stop"))
                self._time_label.setText(tr("player.playing_basic"))
            except Exception:
                self._reset_playing_ui()

    def stop(self) -> None:
        """停止播放并复位进度显示。"""
        if self._player is not None:
            try:
                self._player.stop()
            except Exception:
                pass
        elif self._use_winsound and self._playing:
            import winsound

            try:
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass
        self._reset_playing_ui()

    # -------------------------------------------------------------- 内部槽

    def _on_button_clicked(self) -> None:
        if self._playing:
            self.stop()
        else:
            self.play()

    def _on_tick(self) -> None:
        """轮询播放进度，刷新滑块与时间标签。"""
        if self._player is None:
            return
        position = self._player.position()
        duration = self._player.duration()
        if duration > 0:
            self._slider.setRange(0, duration)
        self._slider.setValue(position)
        self._time_label.setText(f"{_fmt_ms(position)} / {_fmt_ms(duration)}")

    def _on_slider_moved(self, value: int) -> None:
        if self._player is not None:
            self._player.setPosition(value)

    def _on_media_status(self, status) -> None:
        """播放自然结束（EndOfMedia）时复位界面。"""
        if (
            _QT_MULTIMEDIA_OK
            and status == QMediaPlayer.MediaStatus.EndOfMedia
        ):
            self._timer.stop()
            self._reset_playing_ui()
            self.finished.emit()

    def _on_player_error(self, *_args) -> None:
        """后端报错（如无音频设备）时静默复位，避免界面卡在“停止”态。"""
        self._reset_playing_ui()

    def _reset_playing_ui(self) -> None:
        self._timer.stop()
        self._playing = False
        self._play_button.setText(tr("player.play"))
        self._slider.setValue(0)
        if self._source:
            self._time_label.setText("0:00.0")
