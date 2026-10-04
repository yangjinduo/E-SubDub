# -*- coding: utf-8 -*-
"""底部系统监视栏：CPU / 内存 / GPU 折线图 + 百分比（1 秒刷新）。

- CPU/内存：psutil 采样（cpu_percent 非阻塞均值），主线程 QTimer 驱动；
- GPU：nvidia-smi 子进程查询放后台 QThread（避免阻塞 UI 线程），信号回传；
  nvidia-smi 不存在或连续失败时隐藏 GPU 格；
- 折线图 QPainter 自绘（deque 60 点），刷新开销远小于 1% CPU。
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from collections import deque

import psutil
from PySide6.QtCore import Qt, QThread, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout

from app.core.i18n import tr

# 折线图保留的采样点数
_SAMPLE_COUNT = 60
# GPU 查询连续失败多少次后隐藏 GPU 格
_GPU_FAIL_LIMIT = 3


class _GpuSampleThread(QThread):
    """后台轮询 nvidia-smi（利用率 / 显存），每秒一次。"""

    sampled = Signal(float, float, float)  # 利用率%, 显存已用 MB, 显存总量 MB
    failed = Signal()

    def __init__(self, interval_ms: int = 1000, parent=None) -> None:
        super().__init__(parent)
        self._interval = interval_ms
        self._stopped = False

    def run(self) -> None:
        while not self._stopped:
            values = self._query()
            if values is None:
                self.failed.emit()
            else:
                self.sampled.emit(*values)
            # 分段睡眠，保证 stop() 后能及时退出
            for _ in range(10):
                if self._stopped:
                    return
                self.msleep(max(1, self._interval // 10))

    def stop(self) -> None:
        self._stopped = True

    @staticmethod
    def _query() -> tuple[float, float, float] | None:
        try:
            creation = (
                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=utilization.gpu,memory.used,memory.total",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True, text=True, timeout=2,
                creationflags=creation,
            )
            line = (result.stdout or "").strip().splitlines()[0]
            util, used, total = (float(p.strip()) for p in line.split(",")[:3])
            return util, used, total
        except (OSError, ValueError, IndexError, subprocess.SubprocessError):
            return None


# 已请求停止但 OS 线程尚未退出的 GPU 采样线程：保留 Python 引用，
# 防止 QThread 对象在窗口销毁时被连带销毁（线程仍在运行会触发
# Qt 告警甚至崩溃）；finished 送达后移除
_GPU_THREAD_KEEPALIVE: list[_GpuSampleThread] = []


def _retire_keepalive(thread: _GpuSampleThread) -> None:
    if thread in _GPU_THREAD_KEEPALIVE:
        _GPU_THREAD_KEEPALIVE.remove(thread)


class _Sparkline(QFrame):
    """迷你折线图（0-100 数值序列，最近点在最右侧）。"""

    def __init__(self, color: str, parent=None) -> None:
        super().__init__(parent)
        self._samples: deque[float] = deque(maxlen=_SAMPLE_COUNT)
        self._color = QColor(color)
        self.setMinimumHeight(34)
        self.setObjectName("sparkline")

    def push(self, value: float) -> None:
        self._samples.append(max(0.0, min(100.0, value)))
        self.update()

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width, height = self.width(), self.height()

        # 参考网格：中线（浅色底，半透明黑）
        painter.setPen(QPen(QColor(0, 0, 0, 26), 1))
        painter.drawLine(0, height // 2, width, height // 2)

        if len(self._samples) < 2:
            painter.end()
            return

        step_x = width / (_SAMPLE_COUNT - 1)
        points = []
        count = len(self._samples)
        for i, value in enumerate(self._samples):
            x = width - (count - 1 - i) * step_x
            y = height - 2 - (value / 100.0) * (height - 4)
            points.append((x, y))

        painter.setPen(QPen(self._color, 1.6))
        for i in range(len(points) - 1):
            painter.drawLine(
                int(points[i][0]), int(points[i][1]),
                int(points[i + 1][0]), int(points[i + 1][1]),
            )
        # 末端点
        painter.setBrush(self._color)
        painter.drawEllipse(
            int(points[-1][0]) - 2, int(points[-1][1]) - 2, 4, 4,
        )
        painter.end()


class _MonitorCell(QFrame):
    """单个监视格：名称 + 百分比 + 折线图 + 副文本（显存/容量）。"""

    def __init__(self, name: str, color: str, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("monitorCell")

        self._name_label = QLabel(name)
        self._name_label.setObjectName("monitorName")
        self._value_label = QLabel("--%")
        self._value_label.setObjectName("monitorValue")
        self._sub_label = QLabel("")
        self._sub_label.setObjectName("monitorSub")

        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.addWidget(self._name_label)
        head.addStretch(1)
        head.addWidget(self._sub_label)
        head.addWidget(self._value_label)

        self._sparkline = _Sparkline(color)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(4)
        layout.addLayout(head)
        layout.addWidget(self._sparkline, 1)

    def update_value(self, percent: float, sub: str = "") -> None:
        self._value_label.setText(f"{percent:.0f}%")
        self._sub_label.setText(sub)
        self._sparkline.push(percent)


class SystemMonitorBar(QFrame):
    """系统监视栏（主窗口底部一行）。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._gpu_fail_count = 0
        self._gpu_thread: _GpuSampleThread | None = None

        self._cpu_cell = _MonitorCell(tr("monitor.cpu"), "#3B82F6")
        self._mem_cell = _MonitorCell(tr("monitor.memory"), "#10B981")
        self._gpu_cell = _MonitorCell(tr("monitor.gpu"), "#F59E0B")

        layout = QHBoxLayout(self)
        # 无外层卡片，留白交给主窗口布局统一控制（16 边距 + 12 行距）
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._cpu_cell, 1)
        layout.addWidget(self._mem_cell, 1)
        layout.addWidget(self._gpu_cell, 1)

        # CPU 首次调用建立基线（interval=None 非阻塞）
        psutil.cpu_percent(interval=None)

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._sample_host)
        self._timer.start()

        # GPU：无 nvidia-smi 直接隐藏；否则后台线程轮询
        if shutil.which("nvidia-smi"):
            self._gpu_thread = _GpuSampleThread(1000, self)
            self._gpu_thread.sampled.connect(self._on_gpu_sampled)
            self._gpu_thread.failed.connect(self._on_gpu_failed)
            self._gpu_thread.start()
        else:
            self._gpu_cell.hide()
            self._layout_without_gpu()

    # ------------------------------------------------------------------ 采样

    def _sample_host(self) -> None:
        """CPU / 内存采样（主线程，开销毫秒级）。"""
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        mem_total_gb = mem.total / (1024 ** 3)
        mem_used_gb = mem.used / (1024 ** 3)
        self._cpu_cell.update_value(cpu)
        self._mem_cell.update_value(
            mem.percent, f"{mem_used_gb:.1f}/{mem_total_gb:.1f} GB"
        )

    def _on_gpu_sampled(self, util: float, used_mb: float, total_mb: float) -> None:
        self._gpu_fail_count = 0
        self._gpu_cell.update_value(util, f"{used_mb:.0f}/{total_mb:.0f} MB")

    def _on_gpu_failed(self) -> None:
        self._gpu_fail_count += 1
        if self._gpu_fail_count >= _GPU_FAIL_LIMIT and self._gpu_thread is not None:
            self._gpu_thread.stop()
            self._gpu_thread = None
            self._gpu_cell.hide()
            self._layout_without_gpu()

    def _layout_without_gpu(self) -> None:
        """隐藏 GPU 格后让 CPU / 内存两格平分宽度。"""
        self._gpu_cell.setVisible(False)

    # ------------------------------------------------------------------ 生命周期

    def stop(self) -> None:
        """停止采样与后台线程（语言切换重建 / 应用退出时调用）。"""
        self._timer.stop()
        thread = self._gpu_thread
        self._gpu_thread = None
        if thread is None:
            return
        thread.stop()
        # 最坏情况：线程正卡在 timeout=2s 的 nvidia-smi 子进程中，需等
        # 子进程返回后才能看到停止标志，wait 上限须大于该子进程超时
        if thread.wait(3000):
            return
        # 极端情况（nvidia-smi 卡死）：摘除父对象并保留全局引用至线程退出，
        # 避免窗口销毁时连带销毁仍在运行的 QThread
        thread.setParent(None)
        _GPU_THREAD_KEEPALIVE.append(thread)
        thread.finished.connect(lambda t=thread: _retire_keepalive(t))
