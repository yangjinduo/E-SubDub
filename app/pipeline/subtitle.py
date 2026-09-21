# -*- coding: utf-8 -*-
"""SRT 字幕生成：把 Segment 列表写成单语/双语 SRT 文件。

支持三种布局：
- mono: 仅一行目标语文本（配音开启/已翻译场景；target_text 为空时回退原文）
- bilingual_target_top: 双语，上行译文、下行原文
- bilingual_source_top: 双语，上行原文、下行译文
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 字幕布局模式常量
MODE_MONO = "mono"
MODE_BILINGUAL_TARGET_TOP = "bilingual_target_top"
MODE_BILINGUAL_SOURCE_TOP = "bilingual_source_top"

_VALID_MODES = (MODE_MONO, MODE_BILINGUAL_TARGET_TOP, MODE_BILINGUAL_SOURCE_TOP)


def format_srt_time(seconds: float) -> str:
    """秒数 → SRT 时间戳 "HH:MM:SS,mmm"（毫秒四舍五入，负数按 0 处理）。"""
    if seconds < 0:
        seconds = 0.0
    total_ms = int(round(seconds * 1000))
    hours, rem = divmod(total_ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def _clean(text) -> str:
    """去首尾空白；非字符串/None 视为空。"""
    if not isinstance(text, str):
        return ""
    return text.strip()


def _build_text_lines(seg, mode: str) -> list[str]:
    """按模式构造一条字幕的文本行（已剔除空行）。

    - mono: target_text 为空回退 text（配音关闭且未翻译的场景）；
    - bilingual_*: 译文/原文各占一行，某一方为空则跳过该行。
    """
    text = _clean(getattr(seg, "text", ""))
    target = _clean(getattr(seg, "target_text", ""))
    if mode == MODE_MONO:
        first = target if target else text
        return [first] if first else []
    if mode == MODE_BILINGUAL_TARGET_TOP:
        candidates = (target, text)
    elif mode == MODE_BILINGUAL_SOURCE_TOP:
        candidates = (text, target)
    else:
        raise ValueError(f"未知字幕模式: {mode!r}，可选值 {list(_VALID_MODES)}")
    return [c for c in candidates if c]


def generate_srt(
    segments: list,
    srt_path: str,
    mode: str,
    source_lang: str = "",
    target_lang: str = "",
) -> str:
    """把 Segment 列表写成 SRT 文件，返回 srt_path。

    - 跳过 deleted=True 的段；文本全空的段跳过；
    - 序号从 1 递增，时间轴 "HH:MM:SS,mmm --> HH:MM:SS,mmm"；
    - 相邻字幕重叠时（下一行 start < 当前行 end），当前行 end 裁剪为下一行 start，
      避免播放器显示异常；
    - source_lang/target_lang 预留（供上层扩展语言标记），当前 SRT 正文未使用。
    """
    if mode not in _VALID_MODES:
        raise ValueError(f"未知字幕模式: {mode!r}，可选值 {list(_VALID_MODES)}")

    # 过滤已删除段
    active = [seg for seg in segments if not bool(getattr(seg, "deleted", False))]

    blocks: list[str] = []
    index = 1
    for i, seg in enumerate(active):
        start = float(getattr(seg, "start", 0.0))
        end = float(getattr(seg, "end", 0.0))
        # 相邻重叠：裁剪当前行结束时间为下一行起始
        if i + 1 < len(active):
            next_start = float(getattr(active[i + 1], "start", 0.0))
            if next_start < end:
                end = next_start

        text_lines = _build_text_lines(seg, mode)
        if not text_lines:
            continue

        timeline = f"{format_srt_time(start)} --> {format_srt_time(end)}"
        blocks.append(f"{index}\n{timeline}\n" + "\n".join(text_lines))
        index += 1

    content = "\n\n".join(blocks) + ("\n" if blocks else "")
    with open(srt_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    logger.info("生成 SRT(%s): %s 条字幕 → %s", mode, index - 1, srt_path)
    return srt_path
