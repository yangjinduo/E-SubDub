"""开源软件声明窗口：渲染随包分发的 THIRD-PARTY-NOTICES-{CN,EN}.html。

声明文件位于 APP_ROOT（开发态=项目根；冻结态=安装目录，由
build_windows.ps1 复制进 onedir 发行目录），页脚「开源声明」
入口打开本窗口。按当前界面语言选择中文 / 英文版，首选语言版本
缺失时回退另一语言版本。HTML 使用 QTextBrowser 兼容的子集
（内联样式 + 表格属性），外部链接经系统默认浏览器打开。

深浅主题适配：声明 HTML 内嵌的是浅色配色（浏览器直接打开时的
观感），窗口加载时把其中的固定浅色按当前主题令牌
（style.tokens()）替换——浅色主题为恒等映射，深色主题换成对应
深色令牌；链接色由 QPalette.Link 提供，无需处理。
"""

import logging
import re

from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from app.core.config import AppConfig
from app.core.i18n import get_language, tr
from app.ui import style

logger = logging.getLogger(__name__)

# 界面语言 → 声明文件名（与 build_windows.ps1 随包复制的文件保持一致）
_NOTICE_FILENAMES = {
    "zh-CN": "THIRD-PARTY-NOTICES-CN.html",
    "en": "THIRD-PARTY-NOTICES-EN.html",
}


# 声明 HTML 固定浅色 → 主题令牌（令牌值即替换后的颜色）
_COLOR_TOKEN_MAP = {
    "#1F2937": "text",         # 正文
    "#6B7280": "text_muted",   # 次要说明
    "#9CA3AF": "text_faint",   # 页脚弱化文字
    "#F3F4F6": "window_bg",    # 表头行底色
    "#E5E7EB": "card_border",  # 表格边框
    "#F9FAFB": "card_bg",      # 附录许可证全文 pre 块底色（bilibili 协议附录）
}


def _apply_theme_colors(html: str) -> str:
    """把声明 HTML 中的固定浅色替换为当前主题令牌色（单次遍历，避免交叉污染）。"""
    tokens = style.tokens()
    mapping = {hex_color: tokens[name] for hex_color, name in _COLOR_TOKEN_MAP.items()}
    return re.sub(
        r"#[0-9A-Fa-f]{6}",
        lambda m: mapping.get(m.group(0).upper(), m.group(0)),
        html,
    )


class OpenSourceNoticeDialog(QDialog):
    """只读浏览 THIRD-PARTY-NOTICES-{CN,EN}.html；文件缺失时给出可定位的提示。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle(tr("main.open_source_notice"))
        self.resize(880, 640)

        self._browser = QTextBrowser()
        self._browser.setOpenExternalLinks(True)
        notice_path = self._resolve_notice_path()
        if notice_path is not None:
            self._browser.setHtml(_apply_theme_colors(notice_path.read_text(encoding="utf-8")))
        else:
            # 理论上不可达（随包分发且含回退）；保留出口便于冻结包缺文件时自查
            self._browser.setPlainText(
                tr("main.open_source_missing").format(
                    path=AppConfig.APP_ROOT / _NOTICE_FILENAMES["en"]
                )
            )
            logger.warning(
                "开源声明文件缺失：%s",
                ", ".join(str(AppConfig.APP_ROOT / name) for name in _NOTICE_FILENAMES.values()),
            )

        close_button = QPushButton(tr("common.close"))
        close_button.clicked.connect(self.accept)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)
        layout.addWidget(self._browser, 1)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

    @staticmethod
    def _resolve_notice_path():
        """按当前界面语言选声明文件；首选缺失时回退另一语言版本。"""
        preferred = _NOTICE_FILENAMES.get(get_language(), _NOTICE_FILENAMES["zh-CN"])
        fallback = next(name for name in _NOTICE_FILENAMES.values() if name != preferred)
        for name in (preferred, fallback):
            path = AppConfig.APP_ROOT / name
            if path.exists():
                return path
        return None

