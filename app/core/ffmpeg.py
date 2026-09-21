"""ffmpeg 可执行文件路径定位。"""

import platform
import sys
from pathlib import Path


def get_app_root() -> Path:
    """返回应用根目录（项目根，即 app 包的上一级目录）。"""
    return Path(__file__).resolve().parents[2]


def get_ffmpeg_path() -> "str | None":
    """按平台返回内置 ffmpeg 可执行文件的绝对路径，找不到返回 None。

    - Windows: libs/windows/ffmpeg.exe
    - macOS (arm64): libs/macos/Arm/ffmpeg
    - macOS (x86_64): libs/macos/Intel/ffmpeg
    """
    root = get_app_root()

    if sys.platform == "win32":
        relative = Path("libs") / "windows" / "ffmpeg.exe"
    elif sys.platform == "darwin":
        if platform.machine() == "arm64":
            relative = Path("libs") / "macos" / "Arm" / "ffmpeg"
        else:
            relative = Path("libs") / "macos" / "Intel" / "ffmpeg"
    else:
        return None

    path = root / relative
    if path.is_file():
        return str(path)
    return None
