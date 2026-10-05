# -*- coding: utf-8 -*-
"""文本语言自动识别：langid 库 + 中英混排启发式。

文本转语音任务的"自动识别"语言选项使用：优先判中英混排（IndexTTS-2.5
的 zhen 模式），再用 langid 分类；langid 未安装时优雅降级（返回空串，
调用方按默认语言处理并提示安装）。短文本（有效字符 < 20）置信度不足，
不做混排细分，只返回 langid 结果。

IndexTTS-2.5 原生支持：zh / en / ja / es / ar（+ zhen 中英混合）；
其余语言（de / fr / ru / ko…）由模型内部回退到 common 语言 token，
效果不可保证——调用方检测到不支持语种时应弹确认。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# IndexTTS-2.5 原生支持的语言代码（zhen = 中英混合）
SUPPORTED_TTS_LANGS = {"zh", "en", "ja", "es", "ar", "zhen"}

# langid 代码 → 界面显示用的 i18n key（lang.* 已有部分，其余在此补齐）
_LANGID_TO_I18N = {
    "zh": "lang.zh", "en": "lang.en", "ja": "lang.ja", "es": "lang.es",
    "ar": "lang.ar", "ko": "lang.ko", "fr": "lang.fr", "de": "lang.de",
    "ru": "lang.ru", "zhen": "lang.zhen",
}

# 混排判定阈值：汉字与拉丁字母各自的最少出现次数
_MIX_MIN_COUNT = 8


def langid_available() -> bool:
    """langid 库是否已安装。"""
    try:
        import langid  # noqa: F401
    except ImportError:
        return False
    return True


def _is_han(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff" or "\u3400" <= ch <= "\u4dbf"


def _is_latin_letter(ch: str) -> bool:
    return ("a" <= ch <= "z") or ("A" <= ch <= "Z")


def _effective_length(text: str) -> int:
    """有效字符数（汉字/字母/数字，忽略空白与标点）。"""
    return sum(
        1 for ch in text
        if _is_han(ch) or _is_latin_letter(ch) or ch.isdigit()
    )


def _detect_mix(text: str) -> bool:
    """中英混排判定：汉字与拉丁字母均达到阈值（且不出现其他文字系统）。"""
    han = sum(1 for ch in text if _is_han(ch))
    latin = sum(1 for ch in text if _is_latin_letter(ch))
    return han >= _MIX_MIN_COUNT and latin >= _MIX_MIN_COUNT


def detect_text_language(text: str) -> str:
    """识别文本语言，返回语言代码；无法识别返回空串。

    返回值：zhen（中英混排）/ zh / en / ja / es / ar（原生支持），
    或 langid 检出的其他语言代码（de / fr / ru…，调用方需确认），
    短文本或 langid 缺失返回 ""（调用方按默认语言处理）。
    """
    if not text or not text.strip():
        return ""
    # 短文本（< 20 有效字符）不做混排细分，直接交给 langid
    if _effective_length(text) >= 20 and _detect_mix(text):
        return "zhen"
    try:
        import langid
    except ImportError:
        logger.warning("未安装 langid 库（pip install langid），无法自动识别文本语言")
        return ""
    try:
        code, _prob = langid.classify(text)
    except Exception:  # noqa: BLE001 - 分类失败按未识别处理
        logger.exception("langid 语言分类失败")
        return ""
    return str(code)


def language_display_name(code: str) -> str:
    """语言代码 → 界面显示名（经 i18n；未收录的代码原样返回）。"""
    from app.core.i18n import tr

    key = _LANGID_TO_I18N.get(code)
    if key:
        return tr(key)
    return code


def is_tts_supported(code: str) -> bool:
    """该语言代码是否为 IndexTTS-2.5 原生支持。"""
    return code in SUPPORTED_TTS_LANGS
