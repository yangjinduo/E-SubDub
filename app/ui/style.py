"""全局主题样式：浅色 / 深色双主题（QSS + 显式 QPalette），默认深色。

设计要点：
- 不受操作系统深浅色主题影响：Windows 11 系统深色模式会让 Qt 原生样式
  （windows11 style）把未被子 QSS 覆盖的表头、输入框等渲染成黑色。这里
  通过两道防线彻底隔离：
  1) QApplication 固定使用 Fusion 样式（不读取 Windows 深浅色）；
  2) 为 QApplication 显式设置与主题一致的 QPalette，所有颜色角色
     由软件自己决定，与系统配色完全无关。
- 双主题：THEME_TOKENS 以"用途 → 颜色"的令牌表定义浅色与深色两套
  配色，QSS 由 string.Template 按当前主题填充生成，切换主题即重算。
- 主题持久化：与界面语言一致，存于用户配置目录 tasks.json 顶层键
  "theme"（见 app/core/i18n.py 的 get_setting / set_setting）。
- 其他模块的局部样式（表格 QSS、行内 setStyleSheet、自绘颜色）统一从
  tokens() 取当前主题颜色，保证深浅色都清晰可读。
"""

from __future__ import annotations

import re
from pathlib import Path
from string import Template

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer

from app.core.i18n import get_setting, set_setting

# 支持的主题：代码 → 双语显示名（与"语言 / Language"标签一样写死双语，
# 无论用户使用哪种界面语言都能认出此处修改界面主题）
SUPPORTED_THEMES: dict[str, str] = {
    "light": "浅色 / Light",
    "dark": "深色 / Dark",
}

# 默认深色主题（用户未持久化过偏好时）
DEFAULT_THEME = "dark"

# ---------------------------------------------------------------- 主题令牌

# 每个令牌对应 QSS / 自绘中的一个具体用途；浅色值取自原浅色主题，
# 深色值为同语义的深色配色（gray-800/900 系卡片 + 蓝色系强调）。
_THEME_TOKENS: dict[str, dict[str, str]] = {
    "light": {
        # ---- 基础 ----
        "window_bg": "#F3F4F6",        # 主窗口/对话框背景
        "text": "#1F2937",             # 全局正文
        "tooltip_bg": "#FFFFFF",
        "tooltip_border": "#D1D5DB",
        # ---- 通用按钮 ----
        "btn_bg": "#FFFFFF",
        "btn_border": "#D1D5DB",
        "btn_text": "#374151",
        "btn_hover_bg": "#F9FAFB",
        "btn_hover_border": "#9CA3AF",
        "btn_pressed_bg": "#F3F4F6",
        "btn_disabled_bg": "#F3F4F6",
        "btn_disabled_border": "#E5E7EB",
        "btn_disabled_text": "#9CA3AF",
        # ---- 主操作按钮（蓝色） ----
        "primary_bg": "#2563EB",
        "primary_hover_bg": "#1D4ED8",
        "primary_pressed_bg": "#1E40AF",
        "on_accent": "#FFFFFF",
        # ---- 危险操作按钮（描红） ----
        "danger_btn_border": "#FCA5A5",
        "danger_btn_text": "#DC2626",
        "danger_btn_hover_bg": "#FEF2F2",
        "danger_btn_hover_border": "#F87171",
        "danger_btn_pressed_bg": "#FEE2E2",
        # ---- 继续执行按钮（柔和浅绿） ----
        "continue_bg": "#D1FAE5",
        "continue_border": "#A7F3D0",
        "continue_text": "#065F46",
        "continue_hover_bg": "#A7F3D0",
        "continue_hover_border": "#6EE7B7",
        # ---- 链接样式按钮 ----
        "link_text": "#2563EB",
        "link_hover_text": "#1D4ED8",
        # ---- 输入控件 ----
        "input_bg": "#FFFFFF",
        "input_border": "#D1D5DB",
        "input_focus_border": "#2563EB",
        "input_disabled_bg": "#F9FAFB",
        "input_disabled_text": "#4B5563",
        "popup_bg": "#FFFFFF",
        "popup_border": "#E5E7EB",
        "popup_sel_bg": "#EFF6FF",
        "popup_sel_text": "#1D4ED8",
        "check_border": "#D1D5DB",
        "check_bg": "#FFFFFF",
        "check_checked": "#2563EB",
        # ---- 卡片 ----
        "card_bg": "#FFFFFF",
        "card_border": "#E5E7EB",
        "card_hover_border": "#C7D2FE",
        # ---- 文本语义 ----
        "section_title": "#111827",
        "text_strong": "#111827",
        "text_body": "#374151",
        "text_muted": "#6B7280",
        "text_faint": "#9CA3AF",
        "accent_text": "#2563EB",
        "danger_text": "#DC2626",
        "success_text": "#059669",
        "separator_line": "#E5E7EB",
        "dashed_border": "#D1D5DB",
        # ---- 资源卡片 ----
        "feature_name": "#111827",
        "model_name": "#6B7280",
        "ready_badge_bg": "#ECFDF5",
        "ready_badge_text": "#059669",
        "ready_badge_border": "#A7F3D0",
        "notready_badge_bg": "#FEF2F2",
        "notready_badge_text": "#DC2626",
        "notready_badge_border": "#FECACA",
        # ---- 导入区 ----
        "drop_bg": "#F8FAFC",
        "drop_border": "#CBD5E1",
        "drop_hover_bg": "#EFF6FF",
        "card_dnd_hover_bg": "#F8FAFF",
        "import_title": "#334155",
        "import_sub": "#64748B",
        "import_count": "#334155",
        "fileinfo_name": "#1F2937",
        "fileinfo_path": "#9CA3AF",
        "fileinfo_meta": "#4B5563",
        # ---- 流程步骤 ----
        "step_bg": "#FFFFFF",
        "step_border": "#E5E7EB",
        "step_text": "#4B5563",
        "step_active_bg": "#EFF6FF",
        "step_active_border": "#93C5FD",
        "step_active_text": "#1D4ED8",
        "step_done_bg": "#ECFDF5",
        "step_done_border": "#A7F3D0",
        "step_done_text": "#047857",
        "step_done_hover_bg": "#D1FAE5",
        "step_done_hover_border": "#34D399",
        "step_done_hover_text": "#065F46",
        "step_disabled_bg": "#F3F4F6",
        "step_disabled_border": "#E5E7EB",
        "step_disabled_text": "#9CA3AF",
        "step_arrow": "#CBD5E1",
        # ---- 表格 ----
        "table_bg": "#FFFFFF",
        "table_border": "#E5E7EB",
        "table_gridline": "#F3F4F6",
        "table_sel_bg": "#EFF6FF",
        "table_sel_text": "#1D4ED8",
        "header_bg": "#F9FAFB",
        "header_text": "#4B5563",
        "header_border": "#E5E7EB",
        "alt_row_bg": "#FAFBFC",
        "row_text": "#1F2937",
        "row_text_deleted": "#9CA3AF",
        "row_bg": "#FFFFFF",
        "row_bg_deleted": "#F3F4F6",
        # ---- 新建任务按钮（三色） ----
        "dub_bg": "#2563EB", "dub_hover_bg": "#1D4ED8", "dub_pressed_bg": "#1E40AF",
        "sub_bg": "#059669", "sub_hover_bg": "#047857", "sub_pressed_bg": "#065F46",
        "tts_bg": "#7C3AED", "tts_hover_bg": "#6D28D9", "tts_pressed_bg": "#581C87",
        # ---- 系统监视栏 ----
        "monitor_name": "#6B7280",
        "monitor_value": "#111827",
        "monitor_sub": "#9CA3AF",
        # ---- 页脚 ----
        "footer_text": "#9CA3AF",
        "footer_feedback": "#6B7280",
        # ---- 结果查看窗口 ----
        "viewer_path": "#6B7280",
        "viewer_track_title": "#111827",
        # ---- 进度条（QSS 与自绘两种） ----
        "progress_track": "#E5E7EB",
        "progress_chunk": "#2563EB",
        "cell_track_bg": "#EEF2F7",
        "cell_track_fill": "#BFDBFE",
        # ---- 日志框 ----
        "log_bg": "#FFFFFF",
        "log_text": "#334155",
        # ---- 滚动条 ----
        "scroll_handle": "#D1D5DB",
        "scroll_handle_hover": "#9CA3AF",
    },
    "dark": {
        # ---- 基础 ----
        "window_bg": "#111827",
        "text": "#E5E7EB",
        "tooltip_bg": "#1F2937",
        "tooltip_border": "#4B5563",
        # ---- 通用按钮 ----
        "btn_bg": "#1F2937",
        "btn_border": "#4B5563",
        "btn_text": "#D1D5DB",
        "btn_hover_bg": "#283546",
        "btn_hover_border": "#6B7280",
        "btn_pressed_bg": "#374151",
        "btn_disabled_bg": "#111827",
        "btn_disabled_border": "#374151",
        "btn_disabled_text": "#6B7280",
        # ---- 主操作按钮（蓝色） ----
        "primary_bg": "#2563EB",
        "primary_hover_bg": "#3B82F6",
        "primary_pressed_bg": "#1D4ED8",
        "on_accent": "#FFFFFF",
        # ---- 危险操作按钮（描红） ----
        "danger_btn_border": "#B91C1C",
        "danger_btn_text": "#F87171",
        "danger_btn_hover_bg": "#7F1D1D",
        "danger_btn_hover_border": "#DC2626",
        "danger_btn_pressed_bg": "#991B1B",
        # ---- 继续执行按钮（柔和浅绿） ----
        "continue_bg": "#065F46",
        "continue_border": "#047857",
        "continue_text": "#A7F3D0",
        "continue_hover_bg": "#047857",
        "continue_hover_border": "#10B981",
        # ---- 链接样式按钮 ----
        "link_text": "#60A5FA",
        "link_hover_text": "#93C5FD",
        # ---- 输入控件 ----
        "input_bg": "#1F2937",
        "input_border": "#4B5563",
        "input_focus_border": "#3B82F6",
        "input_disabled_bg": "#111827",
        "input_disabled_text": "#9CA3AF",
        "popup_bg": "#1F2937",
        "popup_border": "#374151",
        "popup_sel_bg": "#1E3A8A",
        "popup_sel_text": "#BFDBFE",
        "check_border": "#4B5563",
        "check_bg": "#1F2937",
        "check_checked": "#3B82F6",
        # ---- 卡片 ----
        "card_bg": "#1F2937",
        "card_border": "#374151",
        "card_hover_border": "#4B5563",
        # ---- 文本语义 ----
        "section_title": "#F9FAFB",
        "text_strong": "#F3F4F6",
        "text_body": "#D1D5DB",
        "text_muted": "#9CA3AF",
        "text_faint": "#6B7280",
        "accent_text": "#60A5FA",
        "danger_text": "#F87171",
        "success_text": "#34D399",
        "separator_line": "#374151",
        "dashed_border": "#4B5563",
        # ---- 资源卡片 ----
        "feature_name": "#F3F4F6",
        "model_name": "#9CA3AF",
        "ready_badge_bg": "#064E3B",
        "ready_badge_text": "#34D399",
        "ready_badge_border": "#065F46",
        "notready_badge_bg": "#7F1D1D",
        "notready_badge_text": "#FCA5A5",
        "notready_badge_border": "#991B1B",
        # ---- 导入区 ----
        "drop_bg": "#111827",
        "drop_border": "#475569",
        "drop_hover_bg": "#1E293B",
        "card_dnd_hover_bg": "#17223B",
        "import_title": "#E2E8F0",
        "import_sub": "#94A3B8",
        "import_count": "#E2E8F0",
        "fileinfo_name": "#F3F4F6",
        "fileinfo_path": "#6B7280",
        "fileinfo_meta": "#9CA3AF",
        # ---- 流程步骤 ----
        "step_bg": "#1F2937",
        "step_border": "#374151",
        "step_text": "#9CA3AF",
        "step_active_bg": "#1E3A8A",
        "step_active_border": "#3B82F6",
        "step_active_text": "#93C5FD",
        "step_done_bg": "#064E3B",
        "step_done_border": "#10B981",
        "step_done_text": "#6EE7B7",
        "step_done_hover_bg": "#065F46",
        "step_done_hover_border": "#34D399",
        "step_done_hover_text": "#A7F3D0",
        "step_disabled_bg": "#111827",
        "step_disabled_border": "#374151",
        "step_disabled_text": "#6B7280",
        "step_arrow": "#4B5563",
        # ---- 表格 ----
        "table_bg": "#1F2937",
        "table_border": "#374151",
        "table_gridline": "#374151",
        "table_sel_bg": "#1E3A8A",
        "table_sel_text": "#BFDBFE",
        "header_bg": "#111827",
        "header_text": "#9CA3AF",
        "header_border": "#374151",
        "alt_row_bg": "#111827",
        "row_text": "#E5E7EB",
        "row_text_deleted": "#6B7280",
        "row_bg": "#1F2937",
        "row_bg_deleted": "#111827",
        # ---- 新建任务按钮（三色） ----
        "dub_bg": "#2563EB", "dub_hover_bg": "#3B82F6", "dub_pressed_bg": "#1D4ED8",
        "sub_bg": "#059669", "sub_hover_bg": "#10B981", "sub_pressed_bg": "#047857",
        "tts_bg": "#7C3AED", "tts_hover_bg": "#8B5CF6", "tts_pressed_bg": "#6D28D9",
        # ---- 系统监视栏 ----
        "monitor_name": "#9CA3AF",
        "monitor_value": "#F3F4F6",
        "monitor_sub": "#6B7280",
        # ---- 页脚 ----
        "footer_text": "#6B7280",
        "footer_feedback": "#9CA3AF",
        # ---- 结果查看窗口 ----
        "viewer_path": "#9CA3AF",
        "viewer_track_title": "#F3F4F6",
        # ---- 进度条（QSS 与自绘两种） ----
        "progress_track": "#374151",
        "progress_chunk": "#3B82F6",
        "cell_track_bg": "#111827",
        "cell_track_fill": "#1E3A8A",
        # ---- 日志框 ----
        "log_bg": "#111827",
        "log_text": "#CBD5E1",
        # ---- 滚动条 ----
        "scroll_handle": "#4B5563",
        "scroll_handle_hover": "#6B7280",
    },
}

# ---------------------------------------------------------------- QSS 模板

_QSS_TEMPLATE = Template("""
/* ------------------------------ 基础 ------------------------------ */
QMainWindow, QDialog {
    background: ${window_bg};
}

QWidget {
    font-family: "Microsoft YaHei UI", "微软雅黑", "Segoe UI", sans-serif;
    font-size: 13px;
    color: ${text};
}

QToolTip {
    background: ${tooltip_bg};
    color: ${text};
    border: 1px solid ${tooltip_border};
    padding: 4px 8px;
}

/* ------------------------------ 通用按钮 ------------------------------ */
QPushButton {
    background: ${btn_bg};
    border: 1px solid ${btn_border};
    border-radius: 8px;
    padding: 6px 18px;
    color: ${btn_text};
}
QPushButton:hover {
    background: ${btn_hover_bg};
    border-color: ${btn_hover_border};
}
QPushButton:pressed {
    background: ${btn_pressed_bg};
}
QPushButton:disabled {
    background: ${btn_disabled_bg};
    color: ${btn_disabled_text};
    border-color: ${btn_disabled_border};
}

/* 主操作按钮（开始处理 / 下载 / 删除） */
QPushButton#primaryButton {
    background: ${primary_bg};
    border: 1px solid ${primary_bg};
    color: ${on_accent};
    padding: 10px 36px;
    font-size: 15px;
    font-weight: 600;
}
QPushButton#primaryButton:hover {
    background: ${primary_hover_bg};
    border-color: ${primary_hover_bg};
}
QPushButton#primaryButton:pressed {
    background: ${primary_pressed_bg};
}

/* 危险操作按钮（删除） */
QPushButton#dangerButton {
    background: ${btn_bg};
    border: 1px solid ${danger_btn_border};
    color: ${danger_btn_text};
}
QPushButton#dangerButton:hover {
    background: ${danger_btn_hover_bg};
    border-color: ${danger_btn_hover_border};
}

/* 继续执行按钮（柔和浅绿，与删除按钮形成语义对照） */
QPushButton#continueButton {
    background: ${continue_bg};
    border: 1px solid ${continue_border};
    color: ${continue_text};
}
QPushButton#continueButton:hover {
    background: ${continue_hover_bg};
    border-color: ${continue_hover_border};
}
QPushButton#continueButton:disabled {
    background: ${btn_disabled_bg};
    border-color: ${btn_disabled_border};
    color: ${btn_disabled_text};
}

/* 链接样式按钮（模型主页） */
QPushButton#linkButton {
    background: transparent;
    border: none;
    color: ${link_text};
    padding: 6px 4px;
}
QPushButton#linkButton:hover {
    color: ${link_hover_text};
    text-decoration: underline;
}

/* 小尺寸设置按钮（资源状态栏右侧，纯图标） */
QPushButton#cardSettingButton {
    padding: 0;
    border-radius: 8px;
}

/* ------------------------------ 输入控件 ------------------------------ */
QComboBox {
    background: ${input_bg};
    border: 1px solid ${input_border};
    border-radius: 8px;
    padding: 6px 12px;
    min-width: 150px;
}
QComboBox:hover {
    border-color: ${btn_hover_border};
}
QComboBox:focus {
    border-color: ${input_focus_border};
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background: ${popup_bg};
    border: 1px solid ${popup_border};
    border-radius: 8px;
    selection-background-color: ${popup_sel_bg};
    selection-color: ${popup_sel_text};
    outline: none;
}

QLineEdit {
    background: ${input_bg};
    border: 1px solid ${input_border};
    border-radius: 8px;
    padding: 6px 12px;
    color: ${text};
}
QLineEdit:disabled, QLineEdit[readOnly="true"] {
    background: ${input_disabled_bg};
    color: ${input_disabled_text};
}
QLineEdit:focus {
    border-color: ${input_focus_border};
}

QCheckBox, QRadioButton {
    spacing: 8px;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
}
QCheckBox::indicator {
    border: 1px solid ${check_border};
    border-radius: 4px;
    background: ${check_bg};
}
QCheckBox::indicator:checked {
    background: ${check_checked};
    border-color: ${check_checked};
}
QRadioButton::indicator {
    border: 1px solid ${check_border};
    border-radius: 9px;
    background: ${check_bg};
}
QRadioButton::indicator:checked {
    border: 5px solid ${check_checked};
    background: ${check_bg};
}

/* ------------------------------ 分组卡片 ------------------------------ */
QFrame[card="true"] {
    background: ${card_bg};
    border: 1px solid ${card_border};
    border-radius: 12px;
}

/* 区域标题 */
QLabel#sectionTitle {
    font-size: 15px;
    font-weight: 600;
    color: ${section_title};
}

/* ------------------------------ 资源卡片 ------------------------------ */
QFrame#resourceCard {
    background: ${card_bg};
    border: 1px solid ${card_border};
    border-radius: 10px;
}
QFrame#resourceCard:hover {
    border-color: ${card_hover_border};
}

QLabel#resourceFeatureName {
    font-size: 13px;
    font-weight: 600;
    color: ${feature_name};
}
QLabel#resourceModelName {
    font-size: 12px;
    color: ${model_name};
}

/* 状态徽标 */
QLabel#readyBadge {
    background: ${ready_badge_bg};
    color: ${ready_badge_text};
    border: 1px solid ${ready_badge_border};
    border-radius: 9px;
    padding: 1px 10px;
    font-size: 12px;
    font-weight: 600;
}
QLabel#notReadyBadge {
    background: ${notready_badge_bg};
    color: ${notready_badge_text};
    border: 1px solid ${notready_badge_border};
    border-radius: 9px;
    padding: 1px 10px;
    font-size: 12px;
    font-weight: 600;
}

/* ------------------------------ 导入区 ------------------------------ */
QFrame#importDropArea {
    background: ${drop_bg};
    border: 2px dashed ${drop_border};
    border-radius: 12px;
}
QFrame#importDropArea[dndHover="true"] {
    background: ${drop_hover_bg};
    border-color: ${input_focus_border};
}

/* 拖放悬停高亮（任务窗口的文本卡/参考音频卡） */
QFrame[card="true"][dndHover="true"] {
    border-color: ${input_focus_border};
    background: ${card_dnd_hover_bg};
}

QLabel#importHintTitle {
    font-size: 17px;
    font-weight: 600;
    color: ${import_title};
}
QLabel#importHintSub {
    font-size: 13px;
    color: ${import_sub};
}
/* 有文件时的顶部计数文案（与"删除所选"按钮同行） */
QLabel#importCountLabel {
    font-size: 13px;
    font-weight: 600;
    color: ${import_count};
}

QFrame#fileInfoCard {
    background: ${card_bg};
    border: 1px solid ${card_border};
    border-radius: 10px;
}
QLabel#fileInfoName {
    font-size: 14px;
    font-weight: 600;
    color: ${fileinfo_name};
}
QLabel#fileInfoPath {
    font-size: 12px;
    color: ${fileinfo_path};
}
QLabel#fileInfoMeta {
    font-size: 12px;
    color: ${fileinfo_meta};
}

/* ------------------------------ 流程步骤 ------------------------------ */
StepChip {
    background: ${step_bg};
    border: 1px solid ${step_border};
    border-radius: 12px;
    padding: 4px 12px;
    font-size: 12px;
    color: ${step_text};
}
StepChip[stepState="active"] {
    background: ${step_active_bg};
    border-color: ${step_active_border};
    color: ${step_active_text};
    font-weight: 600;
}
StepChip[stepState="done"] {
    background: ${step_done_bg};
    border-color: ${step_done_border};
    color: ${step_done_text};
}
StepChip[stepState="disabled"] {
    background: ${step_disabled_bg};
    border-color: ${step_disabled_border};
    color: ${step_disabled_text};
}
/* 已完成的流程步骤：可点击，悬停高亮提示可查看结果 */
StepChip[stepState="done"]:hover {
    background: ${step_done_hover_bg};
    border-color: ${step_done_hover_border};
    color: ${step_done_hover_text};
}
QLabel#stepArrow {
    color: ${step_arrow};
    font-size: 12px;
}

/* ------------------------------ 任务列表 / 导入文件表 ------------------------------ */
QTableWidget#taskTable,
QTableWidget#importFileTable {
    background: ${table_bg};
    border: 1px solid ${table_border};
    border-radius: 10px;
    gridline-color: ${table_gridline};
    selection-background-color: ${table_sel_bg};
    selection-color: ${table_sel_text};
}
QTableWidget#taskTable QHeaderView::section,
QTableWidget#importFileTable QHeaderView::section {
    background: ${header_bg};
    color: ${header_text};
    border: none;
    border-bottom: 1px solid ${header_border};
    padding: 8px 6px;
    font-weight: 600;
}
QTableWidget#taskTable QLineEdit,
QTableWidget#importFileTable QLineEdit {
    border: none;
    background: transparent;
}
/* 交替行底色（表格 setAlternatingRowColors） */
QTableWidget#importFileTable:item:alternate {
    background: ${alt_row_bg};
}

/* 新建任务按钮（任务列表头部）：三类任务三色 */
/* 配音与字幕（蓝） */
QPushButton#addDubButton {
    background: ${dub_bg};
    border: 1px solid ${dub_bg};
    border-radius: 8px;
    padding: 6px 18px;
    color: ${on_accent};
    font-weight: 600;
}
QPushButton#addDubButton:hover {
    background: ${dub_hover_bg};
    border-color: ${dub_hover_bg};
}
QPushButton#addDubButton:pressed {
    background: ${dub_pressed_bg};
}
/* 生成字幕（绿） */
QPushButton#addSubButton {
    background: ${sub_bg};
    border: 1px solid ${sub_bg};
    border-radius: 8px;
    padding: 6px 18px;
    color: ${on_accent};
    font-weight: 600;
}
QPushButton#addSubButton:hover {
    background: ${sub_hover_bg};
    border-color: ${sub_hover_bg};
}
QPushButton#addSubButton:pressed {
    background: ${sub_pressed_bg};
}
/* 文本转语音（紫） */
QPushButton#addTtsButton {
    background: ${tts_bg};
    border: 1px solid ${tts_bg};
    border-radius: 8px;
    padding: 6px 18px;
    color: ${on_accent};
    font-weight: 600;
}
QPushButton#addTtsButton:hover {
    background: ${tts_hover_bg};
    border-color: ${tts_hover_bg};
}
QPushButton#addTtsButton:pressed {
    background: ${tts_pressed_bg};
}

/* 行内删除按钮（表格删除列） */
QPushButton#rowDeleteButton {
    background: ${btn_bg};
    border: 1px solid ${danger_btn_border};
    border-radius: 6px;
    padding: 2px 12px;
    font-size: 12px;
    color: ${danger_btn_text};
}
QPushButton#rowDeleteButton:hover {
    background: ${danger_btn_hover_bg};
    border-color: ${danger_btn_hover_border};
}
QPushButton#rowDeleteButton:pressed {
    background: ${danger_btn_pressed_bg};
}
QPushButton#rowDeleteButton:disabled {
    background: ${btn_disabled_bg};
    border-color: ${btn_disabled_border};
    color: ${btn_disabled_text};
}

/* ------------------------------ 系统监视栏（浅色格直接置于窗口背景） ------------------------------ */
QFrame#monitorCell {
    background: ${card_bg};
    border: 1px solid ${card_border};
    border-radius: 10px;
}
QLabel#monitorName {
    color: ${monitor_name};
    font-size: 12px;
}
QLabel#monitorValue {
    color: ${monitor_value};
    font-size: 15px;
    font-weight: 600;
}
QLabel#monitorSub {
    color: ${monitor_sub};
    font-size: 11px;
}
QFrame#sparkline {
    background: transparent;
}

/* ------------------------------ 主窗口页脚（合规提示 / 意见反馈） ------------------------------ */
QLabel#footerText {
    color: ${footer_text};
    font-size: 11px;
}
QLabel#footerFeedback {
    color: ${footer_feedback};
    font-size: 11px;
}

/* ------------------------------ 流程结果查看窗口 ------------------------------ */
QLabel#viewerPathLabel {
    color: ${viewer_path};
    font-size: 12px;
}
QLabel#viewerHint {
    color: ${viewer_path};
}
QLabel#viewerTrackTitle {
    font-size: 13px;
    font-weight: 600;
    color: ${viewer_track_title};
}
QLabel#viewerFallback {
    color: ${viewer_path};
    font-size: 13px;
    padding: 16px;
}

/* ------------------------------ 进度条 ------------------------------ */
QProgressBar {
    background: ${progress_track};
    border: none;
    border-radius: 6px;
    min-height: 10px;
    max-height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: ${progress_chunk};
    border-radius: 6px;
}

/* ------------------------------ 日志框 ------------------------------ */
QPlainTextEdit#logBox {
    background: ${log_bg};
    border: 1px solid ${card_border};
    border-radius: 8px;
    padding: 8px;
    font-family: "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 12px;
    color: ${log_text};
}

/* ------------------------------ 滚动条 ------------------------------ */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: ${scroll_handle};
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: ${scroll_handle_hover};
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
    margin: 2px;
}
QScrollBar::handle:horizontal {
    background: ${scroll_handle};
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
""")

# 共用表格 QSS：编辑/校对对话框与各流程结果查看器的表格
# （原先在 segments_table / translation_review / tts_review /
#   tts_voice_review 各存一份相同副本，统一到此处按主题生成）
_TABLE_QSS_TEMPLATE = Template("""
QTableWidget {
    background: ${table_bg};
    border: 1px solid ${table_border};
    border-radius: 10px;
    gridline-color: ${table_gridline};
    selection-background-color: ${table_sel_bg};
    selection-color: ${table_sel_text};
}
QHeaderView::section {
    background: ${header_bg};
    color: ${header_text};
    border: none;
    border-bottom: 1px solid ${header_border};
    padding: 8px 6px;
    font-weight: 600;
}
""")

# ---------------------------------------------------------------- 主题状态

# 模块导入时加载持久化偏好（在 QApplication 创建之前生效）；
# 非法/缺失回退默认深色主题
_current_theme = get_setting("theme", DEFAULT_THEME)
if _current_theme not in SUPPORTED_THEMES:
    _current_theme = DEFAULT_THEME


def get_theme() -> str:
    """当前主题代码（"light" / "dark"）。"""
    return _current_theme


def set_theme(name: str) -> None:
    """切换当前主题并持久化；非法代码抛 ValueError（不负责刷新界面）。

    界面刷新由调用方负责：QApplication 级用 apply_theme()，局部样式
    通过重建控件或重新 setStyleSheet 生效。
    """
    global _current_theme
    if name not in SUPPORTED_THEMES:
        raise ValueError(f"不支持的主题: {name}（可选: {sorted(SUPPORTED_THEMES)}）")
    if name == _current_theme:
        return
    _current_theme = name
    set_setting("theme", name)


def tokens(theme: str | None = None) -> dict[str, str]:
    """取主题颜色令牌表（默认当前主题）：用途名 → 颜色值。

    供各模块的局部 QSS / setStyleSheet / 自绘颜色按当前主题取色。
    """
    return dict(_THEME_TOKENS[theme or _current_theme])


def table_qss(theme: str | None = None) -> str:
    """共用表格 QSS（按主题生成）。"""
    return _TABLE_QSS_TEMPLATE.substitute(tokens(theme))


# ---------------------------------------------------------------- 应用主题


def build_palette(theme: str | None = None) -> QPalette:
    """按主题构建显式 QPalette。

    这是"不受系统深浅色影响"的关键之一：QSS 未覆盖到的部分
    （消息框、原生弹出、表头兜底等）全部从这个显式调色板取色，
    而不再跟随 Windows 的深/浅色模式。
    """
    c = tokens(theme)
    palette = QPalette()
    for group in (
        QPalette.ColorGroup.Active,
        QPalette.ColorGroup.Inactive,
        QPalette.ColorGroup.Disabled,
    ):
        palette.setColor(group, QPalette.ColorRole.Window, QColor(c["window_bg"]))
        palette.setColor(group, QPalette.ColorRole.WindowText, QColor(c["text"]))
        palette.setColor(group, QPalette.ColorRole.Base, QColor(c["input_bg"]))
        palette.setColor(group, QPalette.ColorRole.AlternateBase, QColor(c["header_bg"]))
        palette.setColor(group, QPalette.ColorRole.Text, QColor(c["text"]))
        palette.setColor(group, QPalette.ColorRole.Button, QColor(c["btn_bg"]))
        palette.setColor(group, QPalette.ColorRole.ButtonText, QColor(c["btn_text"]))
        palette.setColor(group, QPalette.ColorRole.ToolTipBase, QColor(c["tooltip_bg"]))
        palette.setColor(group, QPalette.ColorRole.ToolTipText, QColor(c["text"]))
        palette.setColor(group, QPalette.ColorRole.Highlight, QColor(c["table_sel_bg"]))
        palette.setColor(
            group, QPalette.ColorRole.HighlightedText, QColor(c["table_sel_text"])
        )
        palette.setColor(group, QPalette.ColorRole.Link, QColor(c["link_text"]))
        palette.setColor(group, QPalette.ColorRole.LinkVisited, QColor(c["link_hover_text"]))
        palette.setColor(group, QPalette.ColorRole.PlaceholderText, QColor(c["text_faint"]))
        palette.setColor(group, QPalette.ColorRole.BrightText, QColor(c["text_strong"]))
    return palette


def apply_theme(app) -> None:
    """把当前主题应用到 QApplication（启动时与运行期切换共用）。

    三步隔离系统深浅色：
    1) 固定 Fusion 样式：Qt 的 windows11/vista 原生样式会读取系统
       深色模式把表头、输入框画成黑色，Fusion 不读取系统配色；
    2) 显式设置主题调色板：QSS 兜不住的部分也不跟随系统；
    3) 全局 QSS：由当前主题令牌生成，重设即整体刷新。
    """
    app.setStyle("Fusion")
    app.setPalette(build_palette())
    app.setStyleSheet(_QSS_TEMPLATE.substitute(tokens()))


# ---------------------------------------------------------------- 自绘辅助

def sparkline_grid_color() -> QColor:
    """迷你趋势图网格底线颜色（浅色主题用近黑半透明，深色用近白半透明）。"""
    if _current_theme == "dark":
        return QColor(255, 255, 255, 30)
    return QColor(0, 0, 0, 26)


# ---------------------------------------------------------------- SVG 图标

def themed_icon(svg_path, color_token: str = "text") -> QIcon:
    """按当前主题重新着色的 SVG 图标（单一源文件，深浅色自动适配）。

    做法：读入 SVG 文本，把其中写死的 fill="#xxxxxx" 替换为当前主题的
    令牌颜色，再从字节渲染为 QIcon。相比"浅色/深色各复制一份 SVG"或
    "用折中灰兼顾两种主题"，只需维护一份图标文件；主题切换时界面重建
    会重新调用本函数取到新色。

    svg_path: icons 目录下的 SVG 文件路径；color_token: 颜色令牌名
    （默认 "text"，即正文色：浅色 #1F2937 / 深色 #E5E7EB）。
    """
    raw = Path(svg_path).read_text(encoding="utf-8")
    color = tokens()[color_token]
    recolored = re.sub(
        r'fill\s*=\s*"[#][0-9A-Fa-f]{3,8}"',
        f'fill="{color}"',
        raw,
        flags=re.IGNORECASE,
    )
    renderer = QSvgRenderer(recolored.encode("utf-8"))
    pixmap = QPixmap(renderer.defaultSize())
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)
