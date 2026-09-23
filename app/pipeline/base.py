# -*- coding: utf-8 -*-
"""流水线基础设施：进度回调协议与通用工具。

所有流水线模块函数都接受可选 ``progress: ProgressCallback = None``，
内部在合理粒度上报（进度 0-1，消息为人类可读中文）。
"""

from __future__ import annotations

import contextlib
import json
from typing import Callable, Optional

# 进度回调：参数为 (进度 0~1 的浮点数, 消息字符串)
ProgressCallback = Callable[[float, str], None]


def report_progress(progress: Optional[ProgressCallback], fraction: float, message: str) -> None:
    """安全上报进度；progress 为 None 时忽略，fraction 夹到 [0, 1]。"""
    if progress is None:
        return
    progress(min(max(fraction, 0.0), 1.0), message)


@contextlib.contextmanager
def weights_loading_progress(
    progress: Optional[ProgressCallback],
    frac_from: float,
    frac_to: float,
    label: str,
):
    """把 transformers 权重加载 tqdm 进度转发为 progress 回调（区间线性映射）。

    transformers 加载权重时在 core_model_loading 模块内用 tqdm（终端的
    ``Loading weights: 100%|...| 1600/1600`` 进度条）。加载期间把该模块
    命名空间里的 tqdm 工厂替换为包装版：每个进度条实例的 update 被转发，
    按 [frac_from, frac_to] 映射后上报 ``label xx%``。

    transformers 版本变动（无该模块/tqdm 非 `__call__` 对象）时静默退化，
    进度停留在调用方事先上报的固定点位。取消检查异常照常向上传播。
    """
    if progress is None:
        yield
        return
    try:
        import transformers.core_model_loading as _cml

        _original = _cml.tqdm
        if not callable(_original):
            yield
            return
    except Exception:
        yield
        return

    def _forwarding_tqdm(*args, **kwargs):
        bar = _original(*args, **kwargs)
        original_update = bar.update
        original_close = bar.close

        def _report():
            total = bar.total or 0
            if total > 0:
                ratio = min(bar.n / total, 1.0)
                report_progress(
                    progress,
                    frac_from + (frac_to - frac_from) * ratio,
                    f"{label} {ratio * 100:.0f}%",
                )

        def update(n=1):
            shown = original_update(n)
            _report()
            return shown

        def close():
            # tqdm 迭代内联 n 累加（节流批量调 update），最后一次刷新后的
            # 进度不会补发；close 时 bar.n 已是终值，在此补 100% 落点
            try:
                _report()
            finally:
                original_close()

        bar.update = update
        bar.close = close
        return bar

    _cml.tqdm = _forwarding_tqdm
    try:
        yield
    finally:
        _cml.tqdm = _original


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
