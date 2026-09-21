# -*- coding: utf-8 -*-
"""字幕翻译：LLM 将原文译为目标语言，译文适配配音时长与口语表达。"""

from __future__ import annotations

import json
import logging
from typing import Optional

from app.core.segment import Segment
from app.pipeline.base import ProgressCallback, extract_json_array, report_progress, sliding_windows
from app.pipeline.llm_engine import LLMClient

logger = logging.getLogger(__name__)

# 每批条数 / 相邻批重叠条数
BATCH_SIZE = 15
BATCH_OVERLAP = 3


def _build_system_prompt(source_lang: str, target_lang: str) -> str:
    return (
        f"你是专业的字幕译制与配音适配引擎。用户给出一个 JSON 数组，"
        f"每个元素含 spk/start/end/text 字段。你的任务：\n"
        f"1. 将 text 从{source_lang}翻译为{target_lang}，译文填入 target_text 字段；\n"
        f"2. 译文必须口语化、自然流畅，适合直接用于配音朗读；\n"
        f"3. 每条译文的朗读时长应尽量匹配该条的 end-start 秒：按{target_lang}"
        f"正常语速估算字数，若原文内容明显超出该时长则压缩表达、只保留核心信息；\n"
        f"4. spk/start/end/text 字段原样保留，不得修改，不要添加注释；\n"
        f"5. 只输出同结构的 JSON 数组（每项含 spk/start/end/text/target_text），"
        f"不要输出任何其他文字或代码围栏。"
    )


def translate_segments(
    client: LLMClient,
    segments: list[Segment],
    source_lang: str,
    target_lang: str,
    progress: Optional[ProgressCallback] = None,
) -> list[Segment]:
    """用 LLM 翻译字幕，返回新列表（原列表不变）。

    - deleted 段跳过（不发给 LLM，直接保留）；
    - 滑动窗口分批（每批 15 条、重叠 3 条），重叠区以先成功处理的结果为准；
    - JSON 解析失败重试 1 次，仍失败则该批 target_text 保留原文并记 warning。
    """
    result = [seg.copy() for seg in segments]
    # 只翻译未删除的段
    active_idx = [i for i, s in enumerate(result) if not s.deleted]
    updated = [False] * len(result)
    windows = list(sliding_windows(len(active_idx), BATCH_SIZE, BATCH_OVERLAP))

    system = _build_system_prompt(source_lang, target_lang)
    for wi, (lo, hi) in enumerate(windows):
        idxs = active_idx[lo:hi]
        payload = [
            {"spk": result[i].spk, "start": result[i].start, "end": result[i].end, "text": result[i].text}
            for i in idxs
        ]
        user = "请翻译以下字幕 JSON 数组：\n" + json.dumps(payload, ensure_ascii=False)

        data = None
        for attempt in (1, 2):
            try:
                resp = client.chat(system, user, temperature=0.2)
                parsed = extract_json_array(resp)
                if parsed is not None and len(parsed) == len(idxs):
                    data = parsed
                    break
                logger.warning(
                    "翻译批次 %d 第 %d 次响应结构异常（%s 项，期望 %s 项），重试",
                    wi + 1, attempt, len(parsed) if parsed is not None else "非数组", len(idxs),
                )
            except Exception as exc:  # 网络或 HTTP 异常
                logger.warning("翻译批次 %d 第 %d 次请求失败: %r", wi + 1, attempt, exc)

        if data is None:
            # 该批失败：未处理的段保留原文作 target_text（若后续重叠批成功可覆盖）
            for i in idxs:
                if not updated[i]:
                    result[i].target_text = result[i].text
            logger.warning(
                "翻译批次 %d/%d 失败，保留原文作译文", wi + 1, len(windows)
            )
        else:
            for j, item in enumerate(data):
                gi = idxs[j]
                if updated[gi] or not isinstance(item, dict):
                    continue  # 重叠区以先出现的为准
                target_text = item.get("target_text")
                if isinstance(target_text, str) and target_text.strip():
                    result[gi].target_text = target_text.strip()
                    updated[gi] = True
                elif not updated[gi]:
                    result[gi].target_text = result[gi].text  # 单条缺失时回退原文

        report_progress(
            progress,
            (wi + 1) / len(windows) if windows else 1.0,
            f"字幕翻译批次 {wi + 1}/{len(windows)}",
        )
    return result
