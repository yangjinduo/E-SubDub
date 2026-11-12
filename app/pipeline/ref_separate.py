# -*- coding: utf-8 -*-
"""参考音频人声分离：文本转语音任务的参考清洗（可选增强）。

Demucs 分离去除参考音频中的背景音/环境杂音，仅保留人声供 IndexTTS
克隆（参考质量决定克隆输出质量）。分离结果缓存在源参考音频同目录：

- 命名 {源文件主干}{后缀}.wav，后缀随界面语言（中文 ●纯人声 /
  英文 ●Vocals）；查找时两种后缀都匹配——界面语言切换后旧缓存
  仍能复用；
- 缓存文件比源文件旧（mtime）时视为过期（源被替换过），重新分离
  覆盖——同名不同扩展名的源（123.mp3 / 123.wav）也因此自然消解；
- 源目录不可写（网络盘/受保护目录）时缓存落到调用方给的兜底目录
  （任务工作夹），仅本次可用，不持久共享。

接口：
- is_pure_voice_file(path)：文件名是否已带纯人声标记（选择此类文件
  时任务窗口自动禁用分离开关，防止重复分离）；
- separate_reference_audio(src, fallback_dir, device, ...)：返回
  实际使用的音频路径（缓存命中 / 新分离 / 失败由调用方回退源文件）。
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from typing import Callable, Optional, Sequence

from app.core.i18n import get_language, tr
from app.pipeline.base import ProgressCallback

# 纯人声缓存文件后缀（中文 / 英文；固定集合，生成取当前界面语言）
PURE_VOICE_SUFFIX_ZH = "●纯人声"
PURE_VOICE_SUFFIX_EN = "●Vocals"
PURE_VOICE_SUFFIXES: tuple[str, ...] = (PURE_VOICE_SUFFIX_ZH, PURE_VOICE_SUFFIX_EN)


def is_pure_voice_file(path: str | Path) -> bool:
    """文件名主干是否已带纯人声标记（●纯人声 / ●Vocals）。"""
    stem = Path(path).stem
    return any(suffix in stem for suffix in PURE_VOICE_SUFFIXES)


def _current_suffix() -> str:
    """生成缓存文件用的后缀（随当前界面语言）。"""
    return (
        PURE_VOICE_SUFFIX_ZH if get_language() == "zh-CN"
        else PURE_VOICE_SUFFIX_EN
    )


def _cache_candidates(src: Path) -> list[Path]:
    """源参考音频对应的缓存路径（两种语言后缀都查找）。"""
    return [
        src.with_name(f"{src.stem}{suffix}.wav")
        for suffix in PURE_VOICE_SUFFIXES
    ]


def find_pure_voice_cache(src: Path) -> Optional[Path]:
    """查找可用的纯人声缓存（存在且不早于源文件的 mtime）。"""
    try:
        src_mtime = src.stat().st_mtime
    except OSError:
        return None
    for cand in _cache_candidates(src):
        try:
            if cand.is_file() and cand.stat().st_mtime >= src_mtime:
                return cand
        except OSError:
            continue
    return None


def _ensure_wav(src: Path, work_dir: Path) -> Path:
    """分离输入须为 wav/flac（soundfile 可读）：其余格式经 ffmpeg 转码。"""
    if src.suffix.lower() in (".wav", ".flac"):
        return src
    from app.core.ffmpeg import get_ffmpeg_path

    ffmpeg = get_ffmpeg_path()
    if not ffmpeg:
        raise RuntimeError("未找到内置 ffmpeg，无法解码参考音频")
    import subprocess

    out = work_dir / "ref_input.wav"
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(src), "-acodec", "pcm_s16le", str(out)],
        capture_output=True, timeout=120, check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if proc.returncode != 0 or not out.is_file():
        detail = (proc.stderr or b"")[-300:].decode("utf-8", "replace")
        raise RuntimeError(f"参考音频解码失败：{detail}")
    return out


def separate_reference_audio(
    src: Path,
    fallback_dir: Path,
    device: str,
    progress: Optional[ProgressCallback] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel_exceptions: Sequence[type[BaseException]] = (),
) -> Path:
    """把参考音频分离为纯人声，返回实际使用的音频路径。

    缓存命中直接复用；未命中时分离（非 CPU 设备失败换 CPU 重试一次，
    cancel_exceptions 中列出的取消类异常除外）并把 vocals 写入源同目录
    缓存（不可写时落 fallback_dir）。分离本身失败由调用方处理（文本
    转语音任务按"警告后用原始参考继续"收场）。
    """
    cache = find_pure_voice_cache(src)
    if cache is not None:
        if log is not None:
            log(tr("tts_runner.ref_cache_hit", name=cache.name))
        return cache

    from app.pipeline.separate import separate_vocals

    with tempfile.TemporaryDirectory(prefix="ref_sep_") as tmp:
        tmp_dir = Path(tmp)
        wav_in = _ensure_wav(src, tmp_dir)

        def _run(dev: str) -> str:
            vocals, _no_vocals = separate_vocals(
                str(wav_in), tmp_dir, device=dev, progress=progress,
            )
            return vocals

        try:
            vocals = _run(device)
        except BaseException as exc:
            if device == "cpu" or isinstance(exc, tuple(cancel_exceptions)):
                raise
            if log is not None:
                log(tr("tts_runner.ref_separate_device_fallback",
                       device=device, error=str(exc)))
            vocals = _run("cpu")

        target = src.with_name(f"{src.stem}{_current_suffix()}.wav")
        try:
            shutil.copy2(vocals, target)
            if log is not None:
                log(tr("tts_runner.ref_separated", name=target.name))
            return target
        except OSError:
            # 源目录不可写：缓存落兜底目录（本次可用，不持久共享）；
            # 兜底也失败（磁盘满等）则上抛，由调用方回退原始参考
            fallback = fallback_dir / target.name
            fallback_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(vocals, fallback)
            if log is not None:
                log(tr("tts_runner.ref_cache_fallback", name=fallback.name))
            return fallback
