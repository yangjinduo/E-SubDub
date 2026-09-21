# -*- coding: utf-8 -*-
"""字幕校对：LLM 修复 ASR 识别错误、断句问题，并标记无意义段落。"""

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

SYSTEM_PROMPT = (
    "你是 ASR 字幕校对器。用户给出一个 JSON 数组，每个元素含 spk/start/end/text 字段。"
    "你的任务：\n"
    "1. 修复 text 中的拼写与识别错误（错拼、丢字、同音字等），保持原意不变；\n"
    "2. 修复断句不完整、明显被截断的文本（在合理范围内补全或修剪，不要改写意思）；\n"
    "3. spk/start/end 字段必须原样保留，不得修改；\n"
    "4. 若某段内容无实际意义（如纯音乐标记、呼吸声、哼唱、无意义语气词等转写），"
    "将该段的 deleted 置为 true，否则 deleted 为 false；\n"
    "5. 只输出同结构的 JSON 数组（每项含 spk/start/end/text/deleted），"
    "不要输出任何其他文字、解释或代码围栏。"
)


def proofread_segments(
    client: LLMClient,
    segments: list[Segment],
    progress: Optional[ProgressCallback] = None,
) -> list[Segment]:
    """用 LLM 校对字幕，返回新列表（原列表不变）。

    滑动窗口分批（每批 15 条、重叠 3 条），重叠区以先出现（先成功处理）
    的结果为准；JSON 解析失败重试 1 次，仍失败则该批保留原文本。
    """
    result = [seg.copy() for seg in segments]
    updated = [False] * len(result)
    windows = list(sliding_windows(len(result), BATCH_SIZE, BATCH_OVERLAP))

    for wi, (lo, hi) in enumerate(windows):
        batch = result[lo:hi]
        payload = [
            {"spk": s.spk, "start": s.start, "end": s.end, "text": s.text} for s in batch
        ]
        user = "请校对以下字幕 JSON 数组：\n" + json.dumps(payload, ensure_ascii=False)

        data = None
        for attempt in (1, 2):
            try:
                resp = client.chat(SYSTEM_PROMPT, user, temperature=0.2)
                parsed = extract_json_array(resp)
                if parsed is not None and len(parsed) == len(batch):
                    data = parsed
                    break
                logger.warning(
                    "校对批次 %d 第 %d 次响应结构异常（%s 项，期望 %s 项），重试",
                    wi + 1, attempt, len(parsed) if parsed is not None else "非数组", len(batch),
                )
            except Exception as exc:  # 网络或 HTTP 异常
                logger.warning("校对批次 %d 第 %d 次请求失败: %r", wi + 1, attempt, exc)

        if data is None:
            logger.warning("校对批次 %d/%d 失败，保留原文本", wi + 1, len(windows))
        else:
            for j, item in enumerate(data):
                gi = lo + j
                if updated[gi] or not isinstance(item, dict):
                    continue  # 重叠区以先出现的为准
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    result[gi].text = text.strip()
                deleted = item.get("deleted")
                if deleted is True:
                    result[gi].deleted = True  # 只标记删除，不复活已删段落
                updated[gi] = True

        report_progress(
            progress,
            (wi + 1) / len(windows) if windows else 1.0,
            f"字幕校对批次 {wi + 1}/{len(windows)}",
        )
    return result
