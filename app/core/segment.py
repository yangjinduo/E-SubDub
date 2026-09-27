# -*- coding: utf-8 -*-
"""字幕分段共享数据结构：流水线各阶段（ASR/校对/翻译/TTS）统一使用 Segment。"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace


@dataclass
class Segment:
    """一条字幕分段（时间单位：秒）。

    属性：
        spk: 说话人标签，如 "S01"
        start: 起始时间（秒）
        end: 结束时间（秒）
        text: 原文文本（ASR 输出）
        target_text: 译文文本（LLM 翻译输出）
        deleted: 标记删除（无意义内容或用户手动删除，后续阶段跳过）
        needs_review: 需人工复核标记（如 TTS 生成异常时置位）
        tts_audio_path: 该段 TTS 配音文件路径
        stretch_ratio: TTS 配音伸缩比（语音时长/目标时长，补齐静音不计入；未合成为 None）
    """

    spk: str = "S01"
    start: float = 0.0
    end: float = 0.0
    text: str = ""
    target_text: str = ""
    deleted: bool = False
    needs_review: bool = False
    tts_audio_path: str = ""
    stretch_ratio: float | None = None

    @property
    def duration(self) -> float:
        """分段时长（秒）。"""
        return self.end - self.start

    def copy(self) -> "Segment":
        """返回字段完全相同的副本。"""
        return replace(self)


def segments_to_json(segments: list[Segment]) -> str:
    """将 Segment 列表序列化为 JSON 字符串（UTF-8，字段名：
    spk/start/end/text/target_text/deleted/needs_review/tts_audio_path/stretch_ratio）。"""
    return json.dumps(
        [
            {
                "spk": seg.spk,
                "start": seg.start,
                "end": seg.end,
                "text": seg.text,
                "target_text": seg.target_text,
                "deleted": seg.deleted,
                "needs_review": seg.needs_review,
                "tts_audio_path": seg.tts_audio_path,
                "stretch_ratio": seg.stretch_ratio,
            }
            for seg in segments
        ],
        ensure_ascii=False,
    )


def segments_from_json(data: str) -> list[Segment]:
    """从 JSON 字符串反序列化为 Segment 列表（未知字段忽略，缺失字段用默认值）。"""
    items = json.loads(data)
    if not isinstance(items, list):
        raise ValueError("segments JSON 应为数组")
    segments: list[Segment] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        ratio = item.get("stretch_ratio")
        segments.append(
            Segment(
                spk=str(item.get("spk", "S01")),
                start=float(item.get("start", 0.0)),
                end=float(item.get("end", 0.0)),
                text=str(item.get("text", "")),
                target_text=str(item.get("target_text", "")),
                deleted=bool(item.get("deleted", False)),
                needs_review=bool(item.get("needs_review", False)),
                tts_audio_path=str(item.get("tts_audio_path", "")),
                stretch_ratio=None if ratio is None else float(ratio),
            )
        )
    return segments
