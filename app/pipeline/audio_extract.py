# -*- coding: utf-8 -*-
"""音轨提取：把视频/音频输入统一落到 work_dir/full_audio.wav。"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from app.core.ffmpeg import get_ffmpeg_path
from app.pipeline.base import ProgressCallback, report_progress

# 视频扩展名（按扩展名判断，均通过 ffmpeg 提取音轨）
VIDEO_EXTS = {".mkv", ".mp4", ".mov", ".avi", ".webm"}
# 音频扩展名（wav 直接复制，其余经 ffmpeg 转码为 wav）
AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}

# ffmpeg 输出时长 "Duration: 00:00:09.18" / 进度 "time=00:00:03.05"
_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

OUTPUT_NAME = "full_audio.wav"


def _hhmmss_to_seconds(h: str, m: str, s: str) -> float:
    return int(h) * 3600 + int(m) * 60 + float(s)


def _iter_stderr_lines(stream):
    """按 \\r 或 \\n 分割 ffmpeg 的 stderr 输出（ffmpeg 进度行以 \\r 结尾）。"""
    buf = b""
    while True:
        chunk = stream.read1(4096)
        if not chunk:
            break
        buf += chunk
        parts = re.split(rb"[\r\n]", buf)
        buf = parts.pop()
        for part in parts:
            if part.strip():
                yield part.decode("utf-8", errors="replace")
    if buf.strip():
        yield buf.decode("utf-8", errors="replace")


def extract_audio(
    input_path: str,
    work_dir: Path,
    progress: Optional[ProgressCallback] = None,
) -> str:
    """提取音轨到 work_dir/full_audio.wav，返回 wav 路径。

    - 视频文件（.mkv/.mp4/.mov/.avi/.webm）：ffmpeg 提取音轨，
      转码为 pcm_s16le / 44100Hz / 立体声；
    - .wav 音频：直接复制（下游模块自行重采样）；
    - 其他音频（.mp3/.flac/.m4a 等）：ffmpeg 转码统一为 wav。

    转码过程实时解析 stderr 中的 time=/Duration 上报进度。
    """
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out_path = work_dir / OUTPUT_NAME

    src = Path(input_path)
    if not src.is_file():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")
    suffix = src.suffix.lower()
    if suffix not in VIDEO_EXTS and suffix not in AUDIO_EXTS:
        raise ValueError(f"不支持的输入格式: {suffix}（{input_path}）")

    # wav 音频：直接复制
    if suffix == ".wav":
        report_progress(progress, 0.0, f"复制音频 {src.name}")
        shutil.copy2(src, out_path)
        report_progress(progress, 1.0, "音频复制完成")
        return str(out_path)

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("未找到内置 ffmpeg（libs/windows/ffmpeg.exe）")

    # 视频/非 wav 音频：ffmpeg 统一转码为 44.1kHz 立体声 16bit wav
    report_progress(progress, 0.0, f"提取音轨 {src.name}")
    cmd = [
        ffmpeg, "-y", "-nostdin",
        "-i", str(src),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        str(out_path),
    ]
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    total_duration: Optional[float] = None
    tail_lines: list[str] = []
    try:
        for line in _iter_stderr_lines(proc.stderr):
            if "Duration:" in line and total_duration is None:
                m = _DURATION_RE.search(line)
                if m:
                    total_duration = _hhmmss_to_seconds(*m.groups())
            m = _TIME_RE.search(line)
            if m and total_duration and total_duration > 0:
                cur = _hhmmss_to_seconds(*m.groups())
                report_progress(
                    progress,
                    min(cur / total_duration, 0.99),
                    f"提取音轨中 {cur:.0f}/{total_duration:.0f} 秒",
                )
            tail_lines.append(line)
            if len(tail_lines) > 30:
                tail_lines.pop(0)
    finally:
        proc.stderr.close()
        ret = proc.wait()

    if ret != 0 or not out_path.is_file():
        raise RuntimeError(
            f"ffmpeg 提取音轨失败（退出码 {ret}）:\n" + "\n".join(tail_lines[-10:])
        )
    report_progress(progress, 1.0, "音轨提取完成")
    return str(out_path)
