# -*- coding: utf-8 -*-
"""录制参考音频对话框：麦克风采集 → 实时波形 → 试听 → 保存。

朗读文本为多语言内容（非界面文案）：每种语言一条固定文本，初始语言
由调用方按 TTS 任务语言/检测结果推导（默认中文），用户可在对话框内
切换。采集用 QtMultimedia 的 QAudioSource（44.1kHz 单声道 16bit PCM），
波形按每个读块的峰值绘制滚动柱状图；音频先留在内存并写入临时 WAV
供试听，点"保存"才落到用户配置目录 recorded/（i18n.recorded_refs_dir，
跨更新保留）。时长下限 3 秒（不足禁止保存），无上限：录制由中央圆形
按钮（待录 mic / 录制中 stop 双态配色）手动开始与停止，确保朗读句子
完整、结尾自然收束。
"""

from __future__ import annotations

import os
import re
import struct
import tempfile
import time
import wave
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.config import AppConfig
from app.core.i18n import recorded_refs_dir, tr
from app.ui.style import themed_icon, tokens
from app.ui.widgets.audio_player import AudioPlayerBar

try:  # QtMultimedia 随 PySide6 提供，个别精简环境可能缺失
    from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

    _QT_MULTIMEDIA_OK = True
except ImportError:  # pragma: no cover
    _QT_MULTIMEDIA_OK = False

# 各语言朗读文本（代码同 i18n 的 lang.* 键；一种语言一条，约 8-10 秒）
RECORD_TEXTS: dict[str, str] = {
    "zh": (
        "大家好，今天天气很不错，我们一起聊聊有趣的事情吧。"
        "很高兴认识你，希望这段声音自然又清晰。"
    ),
    "en": (
        "Hello, this is my natural speaking voice. "
        "I'm happy to share it with you today. "
        "Let's create something wonderful together."
    ),
    "ja": "こんにちは、今日はとても良い天気ですね。あなたと出会えて嬉しいです。一緒に楽しい話をしましょう。",
    "ko": "안녕하세요, 오늘 날씨가 참 좋네요. 만나서 반갑습니다. 함께 즐거운 이야기를 나눠요.",
    "fr": "Bonjour, il fait très beau aujourd'hui. Je suis ravi de vous rencontrer. Passons un bon moment ensemble.",
    "de": "Hallo, das Wetter ist heute wirklich schön. Ich freue mich, dich kennenzulernen. Lass uns über schöne Dinge sprechen.",
    "es": "Hola, hoy hace un tiempo estupendo. Me alegra mucho conocerte. Hablemos de cosas interesantes juntos.",
    "ru": "Привет, сегодня замечательная погода. Я рад нашему знакомству. Давай поговорим о чём-нибудь интересном.",
    "ar": "مرحبًا، الطقس جميل اليوم. سعيد بلقائك. دعنا نتحدث عن أمور ممتعة معًا.",
}

_RECORD_MIN_SECONDS = 3.0   # 时长下限（不足禁止保存；无上限，手动停止）
_SAMPLE_RATE = 44100        # 采样率（单声道 16bit PCM，与保存的 WAV 一致）
_REC_BTN_PX = 56            # 圆形录制按钮直径
_REC_ICON_PX = 26           # 按钮图标边长（mic.svg / stop.svg）
_RECORDING_BG = "#DC2626"        # 录制中按钮底色（实底红，两主题通用）
_RECORDING_HOVER_BG = "#B91C1C"  # 录制中悬停底色
_WAVEFORM_BARS = 120        # 波形柱数上限（约每秒 12 块）
_WAVEFORM_GAIN = 8.0        # 波形增益：语音峰值通常只有 0.1~0.3，放大后起伏更明显；嫌大/小改这个值
_ILLEGAL_NAME_CHARS = re.compile(r'[\\/:*?"<>|]')  # Windows 文件名非法字符


class _WaveformView(QWidget):
    """滚动波形：每个采集块的峰值追加为一条竖柱，超宽后丢弃最旧的柱。"""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._levels: list[float] = []
        self.setMinimumHeight(72)

    def clear(self) -> None:
        self._levels.clear()
        self.update()

    def append_level(self, level: float) -> None:
        # 增益放大 + 截断到 [0, 1]：让正常说话音量也能画出大幅起伏
        self._levels.append(max(0.0, min(1.0, level * _WAVEFORM_GAIN)))
        if len(self._levels) > _WAVEFORM_BARS:
            del self._levels[: len(self._levels) - _WAVEFORM_BARS]
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        c = tokens()
        # 中线（静音基准）
        painter.setPen(QColor(c["separator_line"]))
        painter.drawLine(0, self.height() // 2, self.width(), self.height() // 2)
        if not self._levels:
            painter.end()
            return
        bar_color = QColor(c["accent_text"])
        step = self.width() / _WAVEFORM_BARS
        bar_width = max(2.0, step - 2)
        # 柱数不足上限时从左侧铺开，超限后滚动（左侧丢弃最旧的柱）
        for offset, level in enumerate(self._levels[-_WAVEFORM_BARS:]):
            x = offset * step
            half = max(1.0, level * (self.height() / 2 - 3))
            painter.fillRect(
                int(x), int(self.height() / 2 - half),
                int(bar_width), int(half * 2), bar_color,
            )
        painter.end()


class RecordRefDialog(QDialog):
    """录制参考音频对话框。

    信号：
        saved(str)：点"保存"且写盘成功后发射，参数为 WAV 文件完整路径
    """

    saved = Signal(str)

    def __init__(self, initial_lang: str = "zh", parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("tts.record_title"))
        self.setMinimumWidth(520)

        self._pcm = bytearray()          # 已采集的 PCM 字节（16bit 单声道）
        self._record_start = 0.0         # 采集起始时间戳（time.monotonic）
        self._duration = 0.0             # 已录时长（秒）
        self._recording = False
        self._io = None                  # 采集 IO 设备（readyRead 数据源）
        self._preview_path: Path | None = None  # 试听用临时 WAV

        # ---- 顶部：提示 + 语言切换 ----
        hint_label = QLabel(tr("tts.record_hint"))
        hint_label.setStyleSheet(f"color: {tokens()['text_faint']};")
        lang_row = QHBoxLayout()
        lang_label = QLabel(tr("tts.record_text_lang"))
        lang_label.setStyleSheet(f"color: {tokens()['text_body']};")
        self._lang_combo = QComboBox()
        for code in RECORD_TEXTS:
            self._lang_combo.addItem(tr(f"lang.{code}"), code)
        initial = initial_lang if initial_lang in RECORD_TEXTS else "zh"
        self._lang_combo.setCurrentIndex(list(RECORD_TEXTS).index(initial))
        self._lang_combo.currentIndexChanged.connect(self._on_lang_changed)
        lang_row.addWidget(lang_label)
        lang_row.addWidget(self._lang_combo, 1)
        lang_row.addStretch(1)

        # ---- 朗读文本卡 ----
        text_card = QFrame()
        text_card.setProperty("card", True)
        text_layout = QVBoxLayout(text_card)
        text_layout.setContentsMargins(16, 12, 16, 12)
        self._prompt_text = QLabel(RECORD_TEXTS[initial])
        self._prompt_text.setWordWrap(True)
        self._prompt_text.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        text_layout.addWidget(self._prompt_text)

        # ---- 波形 + 计时 ----
        self._waveform = _WaveformView()
        self._time_label = QLabel(tr("tts.record_duration", sec=0.0))
        self._time_label.setStyleSheet(
            f"color: {tokens()['text_body']}; font-size: 13px;"
        )
        self._time_label.setMinimumWidth(72)
        self._time_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        wave_row = QHBoxLayout()
        wave_row.addWidget(self._waveform, 1)
        wave_row.addWidget(self._time_label)

        # ---- 操作行：居中圆形录制按钮（待录 mic / 录制中 stop 双态）----
        action_row = QHBoxLayout()
        self._record_button = QPushButton()
        self._record_button.setFixedSize(_REC_BTN_PX, _REC_BTN_PX)
        self._record_button.setIconSize(QSize(_REC_ICON_PX, _REC_ICON_PX))
        self._record_button.setCursor(Qt.CursorShape.PointingHandCursor)
        self._record_button.clicked.connect(self._on_record_clicked)
        action_row.addStretch(1)
        action_row.addWidget(self._record_button)
        action_row.addStretch(1)

        # ---- 试听 ----
        self._audio_player = AudioPlayerBar()

        # ---- 文件名行 ----
        name_row = QHBoxLayout()
        name_label = QLabel(tr("tts.record_name_label"))
        name_label.setStyleSheet(f"color: {tokens()['text_body']};")
        self._name_edit = QLineEdit(datetime.now().strftime("rec_%Y%m%d_%H%M%S"))
        name_row.addWidget(name_label)
        name_row.addWidget(self._name_edit, 1)

        # ---- 底部按钮 ----
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        self._save_button = QPushButton(tr("common.save"))
        self._save_button.setObjectName("primaryButton")
        self._save_button.setEnabled(False)
        self._save_button.clicked.connect(self._on_save_clicked)
        cancel_button = QPushButton(tr("common.cancel"))
        cancel_button.clicked.connect(self.reject)
        button_row.addWidget(self._save_button)
        button_row.addWidget(cancel_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)
        layout.addWidget(hint_label)
        layout.addLayout(lang_row)
        layout.addWidget(text_card)
        layout.addLayout(wave_row)
        layout.addLayout(action_row)
        layout.addWidget(self._audio_player)
        layout.addLayout(name_row)
        layout.addLayout(button_row)

        # 录音计时显示（100ms 轮询）
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._on_tick)

        # 采集源：默认输入设备 + 固定格式；不可用时禁用录制
        self._source = None
        if _QT_MULTIMEDIA_OK:
            device = QMediaDevices.defaultAudioInput()
            if not device.isNull():
                fmt = QAudioFormat()
                fmt.setSampleRate(_SAMPLE_RATE)
                fmt.setChannelCount(1)
                fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
                self._source = QAudioSource(device, fmt, self)
        if self._source is None:
            self._record_button.setEnabled(False)
            hint_label.setText(
                f"{hint_label.text()}\n{tr('tts.record_unavailable')}"
            )
        self._refresh_record_button()  # 按"可用 / 录制中"态初始化图标与底色

    # ------------------------------------------------------------------ 采集

    def _on_record_clicked(self) -> None:
        """开始/停止切换（停止后再次点击即重新开始，丢弃上一段）。"""
        if self._recording:
            self._stop_recording()
        else:
            self._start_recording()

    def _refresh_record_button(self) -> None:
        """按"设备可用 / 录制中"刷新圆形按钮的图标、底色与提示。

        待录：主操作蓝底 + mic 图标（on_accent 白）；录制中：实底红 +
        stop 图标（红为通用"正在录制"信号，两主题下白图标对比均足够）；
        设备不可用：灰底 + 灰 mic 图标。图标经 themed_icon 按当前主题重
        着色，底色取自主题令牌，深浅主题自动适配。
        """
        c = tokens()
        btn = self._record_button
        if not btn.isEnabled():
            icon = themed_icon(AppConfig.ICONS_DIR / "mic.svg", "btn_disabled_text")
            bg = hover = c["btn_disabled_bg"]
            tip = ""
        elif self._recording:
            icon = themed_icon(AppConfig.ICONS_DIR / "stop.svg", "on_accent")
            bg, hover = _RECORDING_BG, _RECORDING_HOVER_BG
            tip = tr("tts.record_stop")
        else:
            icon = themed_icon(AppConfig.ICONS_DIR / "mic.svg", "on_accent")
            bg, hover = c["primary_bg"], c["primary_hover_bg"]
            tip = tr("tts.record_start")
        btn.setIcon(icon)
        btn.setToolTip(tip)
        btn.setAccessibleName(tip)
        btn.setStyleSheet(
            f"QPushButton {{ border: none; border-radius: {_REC_BTN_PX // 2}px;"
            f" background-color: {bg}; }}"
            f"QPushButton:hover {{ background-color: {hover}; }}"
            f"QPushButton:disabled {{ background-color: {c['btn_disabled_bg']}; }}"
        )

    def _start_recording(self) -> None:
        # 丢弃上一段（先释放试听音源句柄，Windows 上临时文件才能覆盖/删除）
        self._discard_preview()
        self._pcm.clear()
        self._waveform.clear()
        self._duration = 0.0
        self._time_label.setText(tr("tts.record_duration", sec=0.0))
        # 先切"录制中"并同步重绘，再启动采集：QAudioSource.start() 首次
        # 调用需激活音频设备（Windows 实测约 0.9 秒）并阻塞事件循环，若
        # 状态刷新放在其后，按钮换色要等阻塞结束才画得上屏
        self._recording = True
        self._refresh_record_button()
        self._set_controls_recording(True)
        self._record_button.repaint()  # 立即同步重绘，不等事件队列
        self._io = self._source.start()
        if self._io is None:
            self._recording = False
            self._refresh_record_button()
            self._set_controls_recording(False)
            QMessageBox.warning(
                self, self.windowTitle(), tr("tts.record_unavailable")
            )
            return
        # start() 可能返回同一个内部 IO 设备：先断开旧连接避免重复触发
        try:
            self._io.readyRead.disconnect(self._on_ready_read)
        except (TypeError, RuntimeError):
            pass
        self._io.readyRead.connect(self._on_ready_read)
        self._record_start = time.monotonic()  # 从设备真正开始采集计时
        self._timer.start()

    def _stop_recording(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self._timer.stop()
        self._duration = time.monotonic() - self._record_start
        try:
            self._source.stop()  # 停止后 _io 失效
        except Exception:
            pass
        self._io = None
        self._refresh_record_button()
        self._set_controls_recording(False)
        if not self._pcm:
            self._save_button.setEnabled(False)
            return
        self._write_preview()
        if self._duration >= _RECORD_MIN_SECONDS:
            self._time_label.setText(tr("tts.record_duration", sec=self._duration))
            self._save_button.setEnabled(True)
        else:
            self._time_label.setText(
                tr("tts.record_too_short", sec=self._duration)
            )
            self._save_button.setEnabled(False)

    def _on_ready_read(self) -> None:
        """采集到新数据：入 PCM 缓冲、画波形峰值、检查 10 秒上限。"""
        if self._io is None:  # 停止后残留的信号（source.stop 前已断开引用）
            return
        raw = bytes(self._io.readAll())
        if not raw:
            return
        self._pcm.extend(raw)
        count = len(raw) // 2
        peak = 0.0
        if count:
            values = struct.unpack(f"<{count}h", raw[: count * 2])
            peak = max(abs(v) for v in values) / 32768.0
        self._waveform.append_level(peak)
        self._duration = time.monotonic() - self._record_start
        # 无时长上限：由用户点击圆形按钮手动停止，保证朗读句子完整收尾

    def _on_tick(self) -> None:
        """录制中刷新计时显示。"""
        if self._recording:
            self._duration = time.monotonic() - self._record_start
            self._time_label.setText(tr("tts.record_duration", sec=self._duration))

    def _set_controls_recording(self, recording: bool) -> None:
        """录制期间禁用语言切换/文件名/试听，避免状态混乱。"""
        self._lang_combo.setEnabled(not recording)
        self._name_edit.setEnabled(not recording)
        self._audio_player.setEnabled(not recording)

    # ------------------------------------------------------------------ 试听

    def _write_preview(self) -> None:
        """把当前 PCM 写入临时 WAV 供试听条加载。"""
        self._discard_preview()
        if not self._pcm:
            return
        fd, path = tempfile.mkstemp(suffix=".wav", prefix="esubdub_rec_")
        try:
            with os.fdopen(fd, "wb") as f:
                self._write_wav(f)
        except OSError:
            return
        self._preview_path = Path(path)
        self._audio_player.set_source(str(path))

    def _discard_preview(self) -> None:
        """释放试听音源并删除临时文件（重录/保存/关闭前调用）。"""
        self._audio_player.release()  # 先释放文件句柄再删除（WinError 32）
        if self._preview_path is not None:
            # QMediaPlayer 句柄释放是异步的：短暂重试避免临时文件泄漏
            for _ in range(10):
                try:
                    self._preview_path.unlink()
                    break
                except PermissionError:
                    QCoreApplication.processEvents()
                    time.sleep(0.05)
                except OSError:
                    break
            self._preview_path = None

    def _write_wav(self, fileobj) -> None:
        """PCM 字节 → 单声道 16bit WAV。"""
        with wave.open(fileobj, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(_SAMPLE_RATE)
            w.writeframes(bytes(self._pcm))

    # ------------------------------------------------------------------ 保存

    def _on_save_clicked(self) -> None:
        if self._duration < _RECORD_MIN_SECONDS or not self._pcm:
            return
        name = _ILLEGAL_NAME_CHARS.sub("_", self._name_edit.text().strip()).strip(" .")
        if not name:
            QMessageBox.warning(
                self, self.windowTitle(), tr("tts.record_name_required")
            )
            return
        directory = recorded_refs_dir()
        target = directory / f"{name}.wav"
        counter = 2
        while target.exists():  # 重名自动加序号后缀
            target = directory / f"{name} ({counter}).wav"
            counter += 1
        try:
            with open(target, "wb") as f:
                self._write_wav(f)
        except OSError as exc:
            QMessageBox.warning(self, self.windowTitle(), str(exc))
            return
        self._discard_preview()
        self.saved.emit(str(target))
        self.accept()

    # ------------------------------------------------------------------ 其他

    def _on_lang_changed(self, index: int) -> None:
        code = self._lang_combo.itemData(index)
        if code in RECORD_TEXTS:
            self._prompt_text.setText(RECORD_TEXTS[code])

    def done(self, result) -> None:
        """关闭（保存/取消/X）统一清理：停止采集并删除临时试听文件。"""
        if self._recording:
            self._recording = False
            self._timer.stop()
            try:
                self._source.stop()
            except Exception:
                pass
        self._discard_preview()
        super().done(result)
