# -*- coding: utf-8 -*-
"""流水线基础设施：进度回调协议与通用工具。

所有流水线模块函数都接受可选 ``progress: ProgressCallback = None``，
内部在合理粒度上报（进度 0-1，消息为人类可读中文）。
"""

from __future__ import annotations

import json
from typing import Callable, Optional

# 进度回调：参数为 (进度 0~1 的浮点数, 消息字符串)
ProgressCallback = Callable[[float, str], None]


def report_progress(progress: Optional[ProgressCallback], fraction: float, message: str) -> None:
    """安全上报进度；progress 为 None 时忽略，fraction 夹到 [0, 1]。"""
    if progress is None:
        return
    progress(min(max(fraction, 0.0), 1.0), message)


def sliding_windows(total: int, batch_size: int, overlap: int):
    """生成滑动窗口 [start, end) 索引区间，步长 = batch_size - overlap。

    用于 LLM 分批处理字幕：每批 batch_size 条、相邻批重叠 overlap 条，
    保证批边界处的上下文连续。total <= batch_size 时只产出一个窗口。
    """
    if total <= 0:
        return
    if total <= batch_size:
        yield 0, total
        return
    step = max(1, batch_size - overlap)
    start = 0
    while start < total:
        end = min(start + batch_size, total)
        yield start, end
        if end >= total:
            break
        start += step


def extract_json_array(text: str) -> Optional[list]:
    """从 LLM 响应文本中提取 JSON 数组。

    兼容 markdown 代码围栏与前后多余文字；解析失败返回 None。
    """
    if not text:
        return None
    cleaned = text.strip()
    # 去掉 markdown 代码围栏（```json ... ```）
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned
        cleaned = cleaned.rsplit("```", 1)[0]
    left = cleaned.find("[")
    right = cleaned.rfind("]")
    if left == -1 or right <= left:
        return None
    try:
        data = json.loads(cleaned[left : right + 1])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None
