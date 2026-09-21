# -*- coding: utf-8 -*-
"""流水线中段模块（SRT 生成 / 混音 / MKV 封装）独立验证脚本。

不依赖 Qt（offscreen 也不需要）。覆盖：
1. 三种 mode 的 SRT 生成（时间格式、序号、重叠裁剪、deleted/空行跳过）
2. measure_loudness / align_loudness（RMS dBFS 合理性、对齐误差 < 1dB）
3. mix_tracks 冒烟（假 TTS 段 × 背景轨，时长/响度断言、缺失 TTS 跳过）
4. stretch_to_duration（3.5s → 5.0s，误差 ±0.15s）
5. mux_mkv 冒烟（testsrc2 测试视频封装，ffprobe/ffmpeg 解析流布局验证）
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import soundfile as sf

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# ---------- 共享 Segment：优先真实实现，ImportError 时用最小复制品 ----------
try:
    from app.core.segment import Segment

    SEGMENT_SRC = "app.core.segment（真实实现）"
except ImportError:  # 独立可测：共享结构尚未就绪时的最小复制品

    from dataclasses import dataclass

    @dataclass
    class Segment:  # type: ignore[no-redef]
        spk: str = "S01"
        start: float = 0.0
        end: float = 0.0
        text: str = ""
        target_text: str = ""
        deleted: bool = False
        needs_review: bool = False
        tts_audio_path: str = ""

        @property
        def duration(self) -> float:
            return self.end - self.start

    SEGMENT_SRC = "脚本内最小复制品（ImportError 回退）"

from app.core.ffmpeg import get_ffmpeg_path

FFMPEG = get_ffmpeg_path()
WORK = ROOT / "poc" / "mid_workdir"


def ffmpeg_run(*args: str) -> None:
    """运行 ffmpeg（列表参数，路径含空格安全），失败抛异常。"""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
        capture_output=True, text=True, errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg 失败: %s\n%s" % (args, proc.stderr[-2000:]))


def ffmpeg_probe_stderr(path: str) -> str:
    """`ffmpeg -i path`（必然 exit≠0）取 stderr，用于流布局解析。"""
    proc = subprocess.run(
        [FFMPEG, "-hide_banner", "-i", path],
        capture_output=True, text=True, errors="replace",
    )
    return proc.stderr or ""


def check(cond: bool, msg: str, fails: list) -> bool:
    """断言辅助：失败记录到 fails 并打印，返回 cond。"""
    tag = "  ✓ " if cond else "  ✗ "
    print(f"{tag}{msg}")
    if not cond:
        fails.append(msg)
    return cond


def _parse_stream_lines_probe(err: str, kind: str) -> list[tuple]:
    """从 ffmpeg -i stderr 解析 (流号, 语言, 编码, 是否default)。

    语言标记可能缺失（如未打标的原音轨），default 由行内 "(default)" 判定。
    """
    rows = []
    pat = r"^\s*Stream #0:(\d+)(?:\(([\w-]+)\))?: %s: ([\w-]+)" % kind
    for line in err.splitlines():
        m = re.match(pat, line)
        if m:
            rows.append((m.group(1), m.group(2) or "", m.group(3), "(default)" in line))
    return rows


# ---------------------------------------------------------------- test 1
def test_subtitle(fails: list) -> str:
    print("\n[1] subtitle.generate_srt / format_srt_time")
    from app.pipeline.subtitle import format_srt_time, generate_srt

    check(format_srt_time(3661.5) == "01:01:01,500", "format_srt_time(3661.5) == 01:01:01,500", fails)
    check(format_srt_time(9.177) == "00:00:09,177", "format_srt_time(9.177) == 00:00:09,177", fails)
    check(format_srt_time(-5.0) == "00:00:00,000", "format_srt_time(-5) 负数按 0", fails)
    check(format_srt_time(2.9995) == "00:00:03,000", "format_srt_time 毫秒四舍五入", fails)

    # 3 条有效段（含重叠组）+ 1 条 deleted；双语文本；含原文/译文单边为空的行
    segs = [
        Segment(spk="S01", start=0.0, end=3.5, text="Hello world", target_text="你好，世界"),
        Segment(spk="S01", start=3.5, end=6.2, text="", target_text="这是第二句"),
        Segment(spk="S02", start=4.0, end=7.0, text="OVERLAP TEXT", target_text="重叠段"),
        Segment(spk="S01", start=7.0, end=9.0, text="应被跳过", target_text="deleted", deleted=True),
        Segment(spk="S02", start=6.8, end=9.0, text="Last one", target_text=""),
    ]
    mono = generate_srt(segs, str(WORK / "mono.srt"), "mono")
    bi_t = generate_srt(segs, str(WORK / "bi_target_top.srt"), "bilingual_target_top")
    bi_s = generate_srt(segs, str(WORK / "bi_source_top.srt"), "bilingual_source_top")

    m = Path(mono).read_text(encoding="utf-8")
    # mono：4 条（5 段中 1 条 deleted 跳过）；重叠裁剪 6.2→4.0、7.0→6.8
    check(m.split("\n")[0] == "1" and "\n2\n" in m and "\n3\n" in m and "\n4\n" in m and "\n5\n" not in m,
          "mono 序号 1..4 递增，deleted 段未出现", fails)
    check("00:00:00,000 --> 00:00:03,500" in m, "mono 第 1 条时间轴正确", fails)
    check("00:00:03,500 --> 00:00:04,000" in m, "mono 重叠裁剪生效（6.2s 裁为 4.0s）", fails)
    check("00:00:04,000 --> 00:00:06,800" in m, "mono 第 3 条时间轴（7.0s 裁为 6.8s）", fails)
    check("你好，世界" in m and "这是第二句" in m and "Last one" in m,
          "mono target 为空回退 text、text 为空用 target", fails)
    check("OVERLAP" not in m and "应被跳过" not in m, "mono 双语文本未混入原文", fails)

    t = Path(bi_t).read_text(encoding="utf-8")
    lines = t.splitlines()
    # 第 1 块：序号/时间轴/两行文本
    check(lines[0] == "1" and lines[1] == "00:00:00,000 --> 00:00:03,500"
          and lines[2] == "你好，世界" and lines[3] == "Hello world",
          "bilingual_target_top 第 1 条：上行译文、下行原文", fails)
    check("这是第二句\n" in t and t.count("\n\n") >= 2, "bilingual_target_top 空原文行剔除", fails)

    s = Path(bi_s).read_text(encoding="utf-8")
    check("Hello world\n你好，世界" in s, "bilingual_source_top 第 1 条：上行原文、下行译文", fails)
    # 三种 mode 都应以空行分隔块、文件以换行结尾
    for name, content in (("mono", m), ("bilingual_target_top", t), ("bilingual_source_top", s)):
        check(content.endswith("\n") and "\n\n" in content, f"{name} 块间空行分隔 + 末尾换行", fails)

    # 非法 mode 应报错
    try:
        generate_srt(segs[:1], str(WORK / "bad.srt"), "dual")
        check(False, "非法 mode 抛 ValueError", fails)
    except ValueError:
        check(True, "非法 mode 抛 ValueError", fails)
    return bi_t


# ---------------------------------------------------------------- test 2
def test_loudness(fails: list) -> None:
    print("\n[2] mix.measure_loudness / align_loudness")
    from app.pipeline.mix import align_loudness, measure_loudness

    src = str(ROOT / "poc" / "test_audio.wav")
    db = measure_loudness(src)
    print(f"  test_audio.wav 综合 RMS = {db:.2f} dBFS")
    check(-40.0 < db < 0.0, "measure_loudness 返回 -40~0 dB 区间", fails)

    out = align_loudness(src, -16.0, str(WORK / "aligned.wav"))
    db2 = measure_loudness(out)
    print(f"  对齐到 -16.0 dB 后实测 = {db2:.2f} dBFS")
    check(abs(db2 - (-16.0)) < 1.0, "align 后响度差 < 1dB", fails)

    # 大增益防溢出：目标 0 dBFS 时应 clip 不产生 >1 的样本
    out2 = align_loudness(src, 0.0, str(WORK / "aligned_0db.wav"))
    data, _ = sf.read(out2, dtype="float64", always_2d=True)
    check(float(abs(data).max()) <= 1.0, "目标 0 dBFS 时样本 clip 在 ±1.0", fails)
    # 原地覆盖路径
    tmp = WORK / "inplace.wav"
    d, sr = sf.read(src, dtype="float32", always_2d=True)
    sf.write(str(tmp), d, sr, subtype="PCM_16")
    ret = align_loudness(str(tmp), -20.0)
    check(ret == str(tmp) and abs(measure_loudness(str(tmp)) - (-20.0)) < 1.0,
          "out_path=None 原地覆盖生效", fails)


# ---------------------------------------------------------------- test 3
def test_mix(fails: list) -> str:
    print("\n[3] mix.mix_tracks 冒烟")
    from app.pipeline.mix import measure_loudness, mix_tracks

    src = str(ROOT / "poc" / "test_audio.wav")
    tts_a = str(WORK / "tts_a.wav")  # 0-3.5s（22050Hz 单声道，验证重采样路径）
    tts_b = str(WORK / "tts_b.wav")  # 4.5-9s
    ffmpeg_run("-i", src, "-ss", "0", "-t", "3.5", "-c:a", "pcm_s16le", tts_a)
    ffmpeg_run("-i", src, "-ss", "4.5", "-to", "9", "-c:a", "pcm_s16le", tts_b)

    no_vocals = str(ROOT / "poc" / "demucs_out" / "htdemucs" / "test_mixed" / "no_vocals.wav")
    vocals = str(ROOT / "poc" / "demucs_out" / "htdemucs" / "test_mixed" / "vocals.wav")
    out_wav = str(WORK / "mixed.wav")

    tts_segments = [
        SimpleNamespace(start=0.0, end=3.5, tts_audio_path=tts_a, text="你好世界"),
        SimpleNamespace(start=4.5, end=9.0, tts_audio_path=tts_b, text="第二段"),
        SimpleNamespace(start=8.0, end=8.5, tts_audio_path="", text="缺 TTS 应跳过"),
        SimpleNamespace(start=8.2, end=8.4, tts_audio_path=str(WORK / "不存在.wav"), text="文件缺失"),
    ]
    calls: list[tuple] = []
    ret = mix_tracks(tts_segments, no_vocals, out_wav, vocals, progress=lambda f, m: calls.append((f, m)))

    check(ret == out_wav and Path(out_wav).is_file(), "输出文件已生成", fails)
    info_bg, info_out = sf.info(no_vocals), sf.info(out_wav)
    print(f"  背景 {info_bg.duration:.3f}s/{info_bg.samplerate}Hz/{info_bg.channels}ch → "
          f"输出 {info_out.duration:.3f}s/{info_out.samplerate}Hz/{info_out.channels}ch")
    check(abs(info_out.duration - info_bg.duration) < 0.1, "输出与基准轨时长一致", fails)
    check(info_out.samplerate == info_bg.samplerate and info_out.channels == info_bg.channels,
          "输出采样率/声道数与基准轨一致（44.1kHz 立体声）", fails)

    bg_db, voc_db, out_db = measure_loudness(no_vocals), measure_loudness(vocals), measure_loudness(out_wav)
    # 能量近似：背景 + 对齐到原人声响度的 TTS → 期望 RMS² ≈ bg² + voc²
    expected = 10.0 * math.log10(10 ** (bg_db / 10) + 10 ** (voc_db / 10))
    print(f"  RMS: 背景 {bg_db:.1f} / 人声参考 {voc_db:.1f} / 期望 {expected:.1f} / 实测 {out_db:.1f} dBFS")
    check(expected - 3.0 <= out_db <= expected + 4.0, "混音 RMS 与两输入能量和相当", fails)
    data, _ = sf.read(out_wav, dtype="float64", always_2d=True)
    check(float(abs(data).max()) <= 1.0, "输出无溢出（|sample| ≤ 1.0，pcm_s16le）", fails)
    check(len(calls) >= 2 and calls[-1][0] == 1.0, f"progress 按段上报并收尾 1.0（共 {len(calls)} 次）", fails)
    return out_wav


# ---------------------------------------------------------------- test 4
def test_stretch(fails: list) -> None:
    print("\n[4] mix.stretch_to_duration")
    from app.pipeline.mix import stretch_to_duration

    src = str(WORK / "tts_a.wav")  # 3.5s 22050Hz 单声道
    out = str(WORK / "stretched.wav")
    dur = stretch_to_duration(src, 5.0, out)
    real = sf.info(out).duration
    print(f"  3.5s → 目标 5.0s：函数返回 {dur:.3f}s，文件实测 {real:.3f}s")
    check(abs(dur - 5.0) <= 0.15, "返回时长 5.0±0.15s", fails)
    check(abs(real - 5.0) <= 0.15, "文件实际时长 5.0±0.15s", fails)
    check(sf.info(out).samplerate == sf.info(src).samplerate, "采样率保持不变", fails)


# ---------------------------------------------------------------- test 5
def test_mux(srt_path: str, mixed_wav: str, fails: list) -> None:
    print("\n[5] mux.mux_mkv 冒烟")
    from app.pipeline.mux import mux_mkv, probe_duration, probe_streams

    video = str(WORK / "probe_video.mkv")  # 10s testsrc2 + aac 正弦音轨
    ffmpeg_run(
        "-f", "lavfi", "-i", "testsrc2=d=10:s=640x360:r=15",
        "-f", "lavfi", "-i", "sine=frequency=329.6:duration=10",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k", "-shortest", video,
    )
    check(probe_duration(video) > 9.5, f"probe_duration(源视频) = {probe_duration(video):.2f}s", fails)
    streams = probe_streams(video)
    check(sum(1 for s in streams if s["codec_type"] == "video") == 1
          and sum(1 for s in streams if s["codec_type"] == "audio") == 1,
          f"probe_streams(源视频) = {streams}", fails)

    out_mkv = str(WORK / "mux_out.mkv")
    calls: list[tuple] = []
    ret = mux_mkv(
        video, mixed_wav, srt_path, out_mkv,
        progress=lambda f, m: calls.append((f, m)),
    )
    check(ret == out_mkv and Path(out_mkv).is_file(), "MKV 输出已生成", fails)
    check(0 < calls[-1][0] == 1.0, f"封装 progress 收尾 1.0（共 {len(calls)} 次上报）", fails)

    # 流布局验证：解析 ffmpeg -i stderr（无 ffprobe 时的通用路径）
    err = ffmpeg_probe_stderr(out_mkv)
    video_lines = re.findall(r"Stream #0:\d+(?:\([\w-]+\))?: Video: (\w+)", err)
    audio_lines = _parse_stream_lines_probe(err, "Audio")
    sub_lines = _parse_stream_lines_probe(err, "Subtitle")
    print("  输出流布局:")
    for line in err.splitlines():
        if line.strip().startswith("Stream #"):
            print("   ", line.strip())
    check(len(video_lines) == 1 and video_lines[0] == "h264", f"视频流 1 条 h264: {video_lines}", fails)
    check(len(audio_lines) == 2, f"音轨 2 条: {[(a[0], a[1], a[2], a[3]) for a in audio_lines]}", fails)
    if len(audio_lines) == 2:
        idx1, lang1, codec1, def1 = audio_lines[0]
        idx2, lang2, codec2, def2 = audio_lines[1]
        check(codec1 == "aac" and not def1, "原音轨 aac 保留且已取消 default", fails)
        check(codec2 == "aac" and def2, "新配音轨 aac 且 default", fails)
        check(lang2 == "chi", f"新配音轨 language=chi（实际 {lang2}）", fails)
        check(idx2 > idx1, "新配音轨排在原音轨之后", fails)
    check(len(sub_lines) == 1, f"字幕轨 1 条: {[(s[0], s[1], s[2], s[3]) for s in sub_lines]}", fails)
    if len(sub_lines) == 1:
        _, slang, scodec, sdef = sub_lines[0]
        check(scodec in ("subrip", "srt") and sdef and slang == "chi",
              f"字幕轨 srt 且 default 且 language=chi（实际 {scodec}/{sdef}/{slang}）", fails)

    # 中文字幕内容完整进入 MKV：srt 提取比对（流拷回 wav 不适用，转 srt 输出）
    dump = str(WORK / "dump.srt")
    ffmpeg_run("-i", out_mkv, "-map", "0:s:0", "-c:s", "srt", dump)
    raw = Path(dump).read_bytes().decode("utf-8", errors="replace")
    check("你好，世界" in raw and "Last one" in raw, "MKV 内中文字幕内容完整", fails)

    # 边界：无音轨/无字幕轨的纯视频输入
    video_only = str(WORK / "video_only.mkv")
    ffmpeg_run("-f", "lavfi", "-i", "testsrc2=d=2:s=320x240:r=10",
               "-c:v", "libx264", "-preset", "ultrafast", video_only)
    out2 = str(WORK / "mux_video_only.mkv")
    ret2 = mux_mkv(video_only, mixed_wav, srt_path, out2)
    err2 = ffmpeg_probe_stderr(out2)
    a_only = _parse_stream_lines_probe(err2, "Audio")
    s_only = _parse_stream_lines_probe(err2, "Subtitle")
    check(Path(ret2).is_file() and len(a_only) == 1 and a_only[0][3],
          f"无音轨输入：新音轨成为唯一且 default 的音轨 {a_only}", fails)
    check(len(s_only) == 1 and s_only[0][3],
          f"无字幕轨输入：新字幕轨成为唯一且 default 的字幕轨 {s_only}", fails)

    dur = probe_duration(out_mkv)
    check(dur > 9.5, f"probe_duration(输出) = {dur:.2f}s", fails)


# ---------------------------------------------------------------- main
def main() -> int:
    print("=" * 64)
    print("流水线中段模块验证：SRT 生成 / 混音 / MKV 封装")
    print(f"Segment 来源: {SEGMENT_SRC}")
    print(f"工作目录: {WORK}")
    print("=" * 64)
    WORK.mkdir(parents=True, exist_ok=True)
    fails: list[str] = []

    srt_path = test_subtitle(fails)
    test_loudness(fails)
    mixed_wav = test_mix(fails)
    test_stretch(fails)
    test_mux(srt_path, mixed_wav, fails)

    print("\n" + "=" * 64)
    total = len(fails)
    if total == 0:
        print("汇总：全部断言通过 ✓")
    else:
        print(f"汇总：{total} 项断言失败 ✗")
        for f in fails:
            print("  -", f)
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
