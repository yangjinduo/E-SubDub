# -*- coding: utf-8 -*-
"""字幕翻译：Index-Translate-2B 主翻译 + Index-Homura-2B 时长修正（可选）。

主翻译：纯文本逐句批量（编号行输入/输出），使用模型训练对齐的中文指令
模板（"请将以下X文本翻译为Y，直接输出翻译结果，不要进行任何解释"），
输入不含时间戳/说话人/JSON 字段等格式负担，降低小模型幻觉。

时长修正（homura_length_fix）：配音场景下译文预计朗读时长明显超出分段
时长的句子，交 Index-Homura-2B 按"目标音节数"约束重译压缩（仅支持中文
源 → en/ja/ar/es）；仍不达标的句子由 TTS 伸缩兜底，不在此硬性保证。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from app.core.segment import Segment
from app.pipeline.base import ProgressCallback, report_progress
from app.pipeline.llm_engine import LLMClient

logger = logging.getLogger(__name__)

# 每批句数：纯文本行输入/输出，行数即句数（冒烟实测 20 句内格式稳定）
BATCH_SIZE = 20

# Homura 时长修正：目标语言代码 → 指令用语言名（模型仅支持中文源）
HOMURA_TARGETS: dict[str, str] = {
    "en": "英语",
    "ja": "日语",
    "ar": "阿拉伯语",
    "es": "西班牙语",
}

# 目标语速（音节/秒，保守初值）：目标音节数 = 分段时长 × 语速
# （短句偏松、长句偏紧，后续可用 IndexTTS 实测标定微调）
SYLLABLE_RATE: dict[str, float] = {
    "en": 4.0,
    "ja": 7.5,   # 音拍/秒
    "ar": 5.0,
    "es": 6.0,
}

# 触发修正的偏差阈值：当前音节数 > 目标 × 阈值 时才重译
# （译文偏短可由 TTS 补静音/变速兜底，偏长才是硬伤）
DEVIATION_THRESHOLD = 1.15

# 参与时长修正的最短分段（秒）：过短的分段压缩空间小、修正意义不大
_MIN_FIX_DURATION = 0.5


# ---------------------------------------------------------------- 主翻译

def _translate_prompt(source_lang: str, target_lang: str, lines: list[str]) -> str:
    """构造训练对齐格式的翻译指令（编号行正文）。"""
    body = "\n".join(f"{i}. {t}" for i, t in enumerate(lines, 1))
    return (
        f"请将以下{source_lang}文本逐句翻译为{target_lang}，"
        f"每句一行并保留行首编号，直接输出翻译结果，不要进行任何解释：\n{body}"
    )


def _parse_numbered_lines(text: str, expected: int) -> Optional[list[str]]:
    """解析编号行输出：返回译文列表（长度须等于 expected），失败返回 None。

    行首编号须带分隔符（1. / 1、/ 1) 等）才剥离，避免误剥译文自身开头
    的数字（如 "1994年…"）；无编号的裸行整行保留。仅有编号没有译文的
    行（如 "1."）视为空译文，整批判失败。
    """
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    if len(lines) != expected:
        return None
    out: list[str] = []
    for ln in lines:
        if re.fullmatch(r"\d+\s*[.、)）]?", ln):
            return None  # 只有编号没有译文
        m = re.match(r"^\d+\s*[.、)）]\s*(.+)$", ln)
        out.append((m.group(1) if m else ln).strip())
    return out if all(out) else None


def translate_segments(
    client: LLMClient,
    segments: list[Segment],
    source_lang: str,
    target_lang: str,
    progress: Optional[ProgressCallback] = None,
) -> list[Segment]:
    """用 LLM 翻译字幕，返回新列表（原列表不变）。

    - deleted 段跳过（不发给 LLM，译文保持空串，后续阶段跳过）；
    - 每批 BATCH_SIZE 句纯文本行翻译；批次解析失败重试 1 次，仍失败
      则降级为逐句请求；单句失败时该句译文回退原文并记 warning。
    """
    result = [seg.copy() for seg in segments]
    active_idx = [
        i for i, s in enumerate(result)
        if not s.deleted and s.text.strip()
    ]
    batches = [
        (lo, min(lo + BATCH_SIZE, len(active_idx)))
        for lo in range(0, len(active_idx), BATCH_SIZE)
    ]

    for bi, (lo, hi) in enumerate(batches):
        idxs = active_idx[lo:hi]
        lines = [result[i].text for i in idxs]
        user = _translate_prompt(source_lang, target_lang, lines)

        translated: Optional[list[str]] = None
        for attempt in (1, 2):
            try:
                resp = client.chat("", user)
                translated = _parse_numbered_lines(resp, len(idxs))
                if translated is not None:
                    break
                logger.warning(
                    "翻译批次 %d 第 %d 次响应行数不符（期望 %d 句），重试",
                    bi + 1, attempt, len(idxs),
                )
            except Exception as exc:  # 网络或 HTTP 异常
                logger.warning("翻译批次 %d 第 %d 次请求失败: %r", bi + 1, attempt, exc)

        if translated is None:
            # 批量失败：降级逐句请求，单句失败回退原文
            translated = []
            for i in idxs:
                one: Optional[str] = None
                try:
                    resp = client.chat(
                        "", _translate_prompt(source_lang, target_lang, [result[i].text]),
                    )
                    parsed = _parse_numbered_lines(resp, 1)
                    one = parsed[0] if parsed else None
                except Exception as exc:
                    logger.warning("第 %d 句翻译请求失败: %r", i, exc)
                translated.append(one or result[i].text)
            logger.warning("翻译批次 %d/%d 批量失败，已降级逐句翻译", bi + 1, len(batches))

        for i, text in zip(idxs, translated):
            result[i].target_text = text

        report_progress(
            progress,
            (bi + 1) / len(batches) if batches else 1.0,
            f"字幕翻译批次 {bi + 1}/{len(batches)}",
        )
    return result


# ---------------------------------------------------------------- 时长修正（Homura）

def count_syllables(text: str, lang_code: str) -> int:
    """估算文本音节数（启发式，仅用于触发判断与效果对比，非精确值）。

    - en/es：拉丁字母连续元音组数；
    - ja：假名各计 1 拍，汉字按 2 拍估算；
    - ar：按字母数一半估算（短元音不书写，无法直接数音节）。
    """
    if lang_code == "ja":
        kana = sum(1 for c in text if "\u3040" <= c <= "\u30ff")
        kanji = sum(1 for c in text if "\u4e00" <= c <= "\u9fff")
        return kana + 2 * kanji
    if lang_code == "ar":
        letters = sum(1 for c in text if "\u0600" <= c <= "\u06ff")
        return max(1, (letters + 1) // 2)
    return len(re.findall(r"[aeiouyàáâãèéêëìíîïòóôõùúûü]+", text.lower()))


def _homura_prompt(target_name: str, syllables: int, text: str, mora: bool) -> str:
    """构造 Homura 音节约束翻译指令（音节数写进提示词文本，非模板参数）。"""
    unit = "音拍" if mora else "音节"
    return (
        f"请将以下中文文本翻译为{target_name}，译文不超过{syllables}个{unit}，"
        f"直接输出翻译结果，不要进行任何解释：\n{text}"
    )


def homura_length_fix(
    client: LLMClient,
    segments: list[Segment],
    target_lang_code: str,
    target_lang_name: str,
    progress: Optional[ProgressCallback] = None,
) -> tuple[list[Segment], int, int]:
    """用 Index-Homura-2B 修正超长译文，返回（新列表, 修正句数, 超长句数）。

    仅处理预计朗读时长超出分段时长 DEVIATION_THRESHOLD 倍的句子：按目标
    音节数（分段时长 × 语速）重译压缩；重译结果音节数未减少时保留原译文
    （交给 TTS 伸缩兜底）。译文偏短的句子不处理（TTS 可补静音）。
    """
    rate = SYLLABLE_RATE.get(target_lang_code)
    if rate is None:
        return segments, 0, 0

    result = [seg.copy() for seg in segments]
    overlong = [
        (i, seg) for i, seg in enumerate(result)
        if (
            not seg.deleted
            and seg.target_text.strip()
            and seg.duration >= _MIN_FIX_DURATION
            and count_syllables(seg.target_text, target_lang_code)
            > seg.duration * rate * DEVIATION_THRESHOLD
        )
    ]
    if not overlong:
        report_progress(progress, 1.0, "译文时长检查通过，无需修正")
        return result, 0, 0

    mora = target_lang_code == "ja"
    fixed = 0
    for n, (i, seg) in enumerate(overlong):
        target_n = max(3, round(seg.duration * rate))
        prompt = _homura_prompt(target_lang_name, target_n, seg.text, mora)
        new_text = ""
        try:
            new_text = client.chat("", prompt).strip()
        except Exception as exc:
            logger.warning("第 %d 句 Homura 重译失败: %r", i, exc)
        # 仅在确实变短时采纳（音节数按同口径估算对比）
        if new_text and (
            count_syllables(new_text, target_lang_code)
            < count_syllables(seg.target_text, target_lang_code)
        ):
            result[i].target_text = new_text
            fixed += 1
        report_progress(
            progress, (n + 1) / len(overlong),
            f"译文时长修正 {n + 1}/{len(overlong)}",
        )
    return result, fixed, len(overlong)
