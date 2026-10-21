# -*- coding: utf-8 -*-
"""MKV 封装模块：原视频（保留全部原音轨/字幕轨）+ AI 配音轨 + SRT 字幕轨合成。

- 流探测：优先 ffprobe（JSON），环境无 ffprobe 时解析 `ffmpeg -i` 的 stderr；
- map 动态构造：`-map 0` 保留原视频全部流，再追加新音轨与新字幕轨；
  新音轨在输出中的 audio 索引 = 原音轨数，新字幕轨 subtitle 索引 = 原字幕轨数，
  据此设置编码、语言、标题与默认播放标记（disposition）；
- 原音轨流拷贝；若原编码与 MKV 不兼容导致失败，回退为整轨重编码 aac 192k；
- 进度：解析 ffmpeg stderr 的 time= 与总时长之比。
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

# 延迟引用共享结构：app/pipeline/base.py 可能由其他模块并行开发
try:
    from app.pipeline.base import ProgressCallback
except Exception:  # pragma: no cover - base.py 尚未就绪时兜底
    ProgressCallback = None  # type: ignore[assignment,misc]

# ffmpeg -i stderr 中流行示例（ffmpeg 6 及更早）：Stream #0:1(chi): Audio: aac ...
# ffmpeg 7+ 在索引后新增流 ID 段：Stream #0:1[0x2](und): Audio: aac ...
_STREAM_RE = re.compile(
    r"Stream #0:(\d+)(?:\[[^\]]*\])?(?:\([\w-]+\))?: (Video|Audio|Subtitle)"
)
# Duration 行示例：Duration: 00:00:09.18, start: ...
_DURATION_RE = re.compile(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)")
# 进度行示例：time=00:00:05.12 ...
_TIME_RE = re.compile(r"time=(\d+):(\d+):(\d+(?:\.\d+)?)")

# 异常信息保留的 stderr 尾部行数
_ERR_TAIL_LINES = 40


def _ffmpeg_path() -> str:
    """ffmpeg 可执行路径：内置 libs 优先，PATH 兜底。"""
    try:
        from app.core.ffmpeg import get_ffmpeg_path  # 延迟导入，只读引用

        p = get_ffmpeg_path()
        if p:
            return p
    except Exception:  # pragma: no cover - app.core.ffmpeg 不可用时兜底
        pass
    return "ffmpeg"


def _ffprobe_path() -> str | None:
    """ffprobe 可执行路径：PATH 优先，其次 ffmpeg 同目录；找不到返回 None。"""
    exe_name = "ffprobe.exe" if _is_windows() else "ffprobe"
    candidates: list[Path] = []
    ff = _ffmpeg_path()
    if ff and ff != "ffmpeg":
        candidates.append(Path(ff).parent / exe_name)
    import shutil

    which = shutil.which("ffprobe")
    if which:
        candidates.insert(0, Path(which))
    for c in candidates:
        if c.is_file():
            return str(c)
    return None


def _is_windows() -> bool:
    import sys

    return sys.platform == "win32"


def _probe_stderr(media_path: str) -> str:
    """运行 `ffmpeg -i media`（无输出文件，必然退出码非 0）取 stderr。"""
    proc = subprocess.run(
        [_ffmpeg_path(), "-hide_banner", "-i", media_path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return proc.stderr or ""


def probe_streams(media_path: str) -> list[dict]:
    """探测媒体文件的流列表，返回 [{"index": int, "codec_type": str}]。

    优先 ffprobe `-show_entries stream=index,codec_type -of json`；
    无 ffprobe 时解析 `ffmpeg -i` stderr 的 Stream 行（Video/Audio/Subtitle）。
    """
    ffprobe = _ffprobe_path()
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "stream=index,codec_type",
                    "-of", "json", media_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout)
                return [
                    {
                        "index": int(s.get("index", i)),
                        "codec_type": str(s.get("codec_type", "")),
                    }
                    for i, s in enumerate(data.get("streams", []))
                ]
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("ffprobe 探测失败（%s），回退 ffmpeg 解析: %s", exc, media_path)
    stderr = _probe_stderr(media_path)
    streams = [
        {"index": int(m.group(1)), "codec_type": m.group(2).lower()}
        for m in _STREAM_RE.finditer(stderr)
    ]
    return streams


def probe_duration(media_path: str) -> float:
    """取媒体时长（秒），失败返回 0.0。ffprobe 优先，ffmpeg 解析兜底。"""
    ffprobe = _ffprobe_path()
    if ffprobe:
        try:
            proc = subprocess.run(
                [
                    ffprobe, "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "json", media_path,
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if proc.returncode == 0 and proc.stdout.strip():
                dur = json.loads(proc.stdout).get("format", {}).get("duration")
                if dur is not None:
                    return float(dur)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            logger.warning("ffprobe 时长探测失败（%s），回退 ffmpeg 解析: %s", exc, media_path)
    m = _DURATION_RE.search(_probe_stderr(media_path))
    if not m:
        return 0.0
    h, mi, s = int(m.group(1)), int(m.group(2)), float(m.group(3))
    return h * 3600.0 + mi * 60.0 + s


def _build_mux_cmd(
    input_video: str,
    new_audio_wav: str,
    srt_path: str,
    output_mkv: str,
    audio_count: int,
    sub_count: int,
    audio_lang: str,
    subtitle_lang: str,
    audio_title: str,
    reencode_audio: bool = False,
) -> list[str]:
    """构造 ffmpeg 封装命令。

    - 原视频全部流保留（-map 0），追加新音轨（-map 1:a）与新字幕轨（-map 2:s）；
    - 新音轨输出 audio 索引 = audio_count，新字幕输出 subtitle 索引 = sub_count；
    - reencode_audio=False：原音轨流拷贝；True：回退整轨 aac 192k 重编码。
    """
    new_a_idx = audio_count
    new_s_idx = sub_count
    cmd: list[str] = [
        _ffmpeg_path(), "-y", "-hide_banner",
        "-i", input_video,
        "-i", new_audio_wav,
        "-i", srt_path,
        "-map", "0", "-map", "1:a", "-map", "2:s",
        "-c:v", "copy",
    ]
    # 原音轨编码：默认流拷贝（MKV 常见 aac/ac3/dts 均可），失败时回退重编码
    for i in range(audio_count):
        cmd += ["-c:a:%d" % i, "aac" if reencode_audio else "copy"]
    if reencode_audio and audio_count:
        cmd += ["-b:a", "192k"]
    # 新配音轨固定 aac 192k
    cmd += ["-c:a:%d" % new_a_idx, "aac", "-b:a", "192k"]
    # 原字幕轨流拷贝，新字幕轨转 srt
    for i in range(sub_count):
        cmd += ["-c:s:%d" % i, "copy"]
    cmd += ["-c:s:%d" % new_s_idx, "srt"]
    # 新配音轨元数据（语言 + 标题）、新字幕轨语言
    cmd += [
        "-metadata:s:a:%d" % new_a_idx, "language=%s" % audio_lang,
        "-metadata:s:a:%d" % new_a_idx, "title=%s" % audio_title,
        "-metadata:s:s:%d" % new_s_idx, "language=%s" % subtitle_lang,
    ]
    # 原音轨取消默认标记，新配音轨设默认；新字幕轨设默认
    for i in range(audio_count):
        cmd += ["-disposition:a:%d" % i, "0"]
    cmd += [
        "-disposition:a:%d" % new_a_idx, "default",
        "-disposition:s:%d" % new_s_idx, "default",
    ]
    cmd.append(output_mkv)
    return cmd


def _run_ffmpeg_with_progress(
    cmd: list[str], total: float, progress=None
) -> tuple[int, list[str]]:
    """运行 ffmpeg，解析 stderr 的 time= 与总时长之比上报进度。

    返回 (returncode, stderr 尾部行)。
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    tail: list[str] = []
    last_frac = 0.0
    assert proc.stderr is not None
    for line in proc.stderr:
        line = line.rstrip("\r\n")
        tail.append(line)
        if len(tail) > _ERR_TAIL_LINES:
            tail.pop(0)
        m = _TIME_RE.search(line)
        if m and total > 0:
            t = (
                int(m.group(1)) * 3600.0
                + int(m.group(2)) * 60.0
                + float(m.group(3))
            )
            frac = min(t / total, 1.0)
            if frac > last_frac:
                last_frac = frac
                if progress is not None:
                    progress(frac * 0.95, "封装进度 %.0f%%" % (frac * 100))
    returncode = proc.wait()
    return returncode, tail


def mux_mkv(
    input_video: str,
    new_audio_wav: str,
    srt_path: str,
    output_mkv: str,
    audio_lang: str = "chi",
    subtitle_lang: str = "chi",
    audio_title: str = "AI配音",
    progress: "ProgressCallback | None" = None,
) -> str:
    """封装 MKV：原视频（全部流保留）+ 原音轨流拷贝 + AI 配音轨 + SRT 字幕轨。

    - 新配音轨 aac 192k、language=audio_lang、title=audio_title、默认音轨；
      原音轨取消默认标记（无音轨的边界下新音轨即 a:0）；
    - 新字幕轨 srt、language=subtitle_lang、默认字幕轨；
    - 原音轨流拷贝失败（编码与 MKV 不兼容）时回退 aac 192k 重编码重试；
    - progress 按封装进度（time=/总时长）上报。
    返回 output_mkv。
    """
    for p in (input_video, new_audio_wav, srt_path):
        if not Path(p).is_file():
            raise FileNotFoundError(f"封装输入不存在: {p}")

    streams = probe_streams(input_video)
    if not streams:
        raise RuntimeError(f"无法探测输入视频流信息（文件损坏或非媒体文件）: {input_video}")
    audio_count = sum(1 for s in streams if s["codec_type"] == "audio")
    sub_count = sum(1 for s in streams if s["codec_type"] == "subtitle")
    total = probe_duration(input_video)
    logger.info(
        "封装 MKV: 原流=%d (音轨 %d / 字幕 %d), 总时长 %.2fs → %s",
        len(streams), audio_count, sub_count, total, output_mkv,
    )

    cmd = _build_mux_cmd(
        input_video, new_audio_wav, srt_path, output_mkv,
        audio_count, sub_count, audio_lang, subtitle_lang, audio_title,
        reencode_audio=False,
    )
    returncode, tail = _run_ffmpeg_with_progress(cmd, total, progress)
    if returncode != 0:
        # 原音轨流拷贝失败（原编码与 MKV 不兼容）→ 回退整轨 aac 192k 重编码
        logger.warning(
            "流拷贝封装失败（exit=%d），回退重编码原音轨 aac 192k 重试:\n%s",
            returncode, "\n".join(tail[-10:]),
        )
        if progress is not None:
            progress(0.0, "流拷贝失败，回退重编码原音轨")
        cmd = _build_mux_cmd(
            input_video, new_audio_wav, srt_path, output_mkv,
            audio_count, sub_count, audio_lang, subtitle_lang, audio_title,
            reencode_audio=True,
        )
        returncode, tail = _run_ffmpeg_with_progress(cmd, total, progress)
        if returncode != 0:
            raise RuntimeError(
                "MKV 封装失败（exit=%d）:\n%s" % (returncode, "\n".join(tail))
            )
    if progress is not None:
        progress(1.0, "封装完成")
    return output_mkv
