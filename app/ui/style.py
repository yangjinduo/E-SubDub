"""全局 QSS 样式：浅色现代风格（圆角卡片、细边框、充足留白）。"""

APPLICATION_QSS = """
/* ------------------------------ 基础 ------------------------------ */
QMainWindow, QDialog {
    background: #F3F4F6;
}

QWidget {
    font-family: "Microsoft YaHei UI", "微软雅黑", "Segoe UI", sans-serif;
    font-size: 13px;
    color: #1F2937;
}

QToolTip {
    background: #FFFFFF;
    color: #1F2937;
    border: 1px solid #D1D5DB;
    padding: 4px 8px;
}

/* ------------------------------ 通用按钮 ------------------------------ */
QPushButton {
    background: #FFFFFF;
    border: 1px solid #D1D5DB;
    border-radius: 8px;
    padding: 6px 18px;
    color: #374151;
}
QPushButton:hover {
    background: #F9FAFB;
    border-color: #9CA3AF;
}
QPushButton:pressed {
    background: #F3F4F6;
}
QPushButton:disabled {
    background: #F3F4F6;
    color: #9CA3AF;
    border-color: #E5E7EB;
}

/* 主操作按钮（开始处理 / 下载 / 删除） */
QPushButton#primaryButton {
    background: #2563EB;
    border: 1px solid #2563EB;
    color: #FFFFFF;
    padding: 10px 36px;
    font-size: 15px;
    font-weight: 600;
}
QPushButton#primaryButton:hover {
    background: #1D4ED8;
    border-color: #1D4ED8;
}
QPushButton#primaryButton:pressed {
    background: #1E40AF;
}

/* 危险操作按钮（删除） */
QPushButton#dangerButton {
    background: #FFFFFF;
    border: 1px solid #FCA5A5;
    color: #DC2626;
}
QPushButton#dangerButton:hover {
    background: #FEF2F2;
    border-color: #F87171;
}

/* 链接样式按钮（模型主页） */
QPushButton#linkButton {
    background: transparent;
    border: none;
    color: #2563EB;
    padding: 6px 4px;
}
QPushButton#linkButton:hover {
    color: #1D4ED8;
    text-decoration: underline;
}

/* 小尺寸设置按钮（资源卡片右侧） */
QPushButton#cardSettingButton {
    padding: 3px 12px;
    font-size: 12px;
    border-radius: 6px;
}

/* ------------------------------ 输入控件 ------------------------------ */
QComboBox {
    background: #FFFFFF;
    border: 1px solid #D1D5DB;
    border-radius: 8px;
    padding: 6px 12px;
    min-width: 150px;
}
QComboBox:hover {
    border-color: #9CA3AF;
}
QComboBox:focus {
    border-color: #2563EB;
}
QComboBox::drop-down {
    border: none;
    width: 24px;
}
QComboBox QAbstractItemView {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 8px;
    selection-background-color: #EFF6FF;
    selection-color: #1D4ED8;
    outline: none;
}

QLineEdit {
    background: #FFFFFF;
    border: 1px solid #D1D5DB;
    border-radius: 8px;
    padding: 6px 12px;
    color: #1F2937;
}
QLineEdit:disabled, QLineEdit[readOnly="true"] {
    background: #F9FAFB;
    color: #4B5563;
}
QLineEdit:focus {
    border-color: #2563EB;
}

QCheckBox, QRadioButton {
    spacing: 8px;
}
QCheckBox::indicator, QRadioButton::indicator {
    width: 16px;
    height: 16px;
}
QCheckBox::indicator {
    border: 1px solid #D1D5DB;
    border-radius: 4px;
    background: #FFFFFF;
}
QCheckBox::indicator:checked {
    background: #2563EB;
    border-color: #2563EB;
}
QRadioButton::indicator {
    border: 1px solid #D1D5DB;
    border-radius: 9px;
    background: #FFFFFF;
}
QRadioButton::indicator:checked {
    border: 5px solid #2563EB;
    background: #FFFFFF;
}

/* ------------------------------ 分组卡片 ------------------------------ */
QFrame[card="true"] {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 12px;
}

/* 区域标题 */
QLabel#sectionTitle {
    font-size: 15px;
    font-weight: 600;
    color: #111827;
}

/* ------------------------------ 资源卡片 ------------------------------ */
QFrame#resourceCard {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
}
QFrame#resourceCard:hover {
    border-color: #C7D2FE;
}

QLabel#resourceFeatureName {
    font-size: 13px;
    font-weight: 600;
    color: #111827;
}
QLabel#resourceModelName {
    font-size: 12px;
    color: #6B7280;
}

/* 状态徽标 */
QLabel#readyBadge {
    background: #ECFDF5;
    color: #059669;
    border: 1px solid #A7F3D0;
    border-radius: 9px;
    padding: 1px 10px;
    font-size: 12px;
    font-weight: 600;
}
QLabel#notReadyBadge {
    background: #FEF2F2;
    color: #DC2626;
    border: 1px solid #FECACA;
    border-radius: 9px;
    padding: 1px 10px;
    font-size: 12px;
    font-weight: 600;
}

/* 推理设备角标 */
QLabel#deviceLabelTitle {
    font-size: 12px;
    color: #6B7280;
}
QLabel#deviceLabelValue {
    font-size: 13px;
    font-weight: 600;
    color: #374151;
}

/* ------------------------------ 导入区 ------------------------------ */
QFrame#importDropArea {
    background: #F8FAFC;
    border: 2px dashed #CBD5E1;
    border-radius: 12px;
}
QFrame#importDropArea[dndHover="true"] {
    background: #EFF6FF;
    border-color: #2563EB;
}

QLabel#importHintTitle {
    font-size: 17px;
    font-weight: 600;
    color: #334155;
}
QLabel#importHintSub {
    font-size: 13px;
    color: #64748B;
}

QFrame#fileInfoCard {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
}
QLabel#fileInfoName {
    font-size: 14px;
    font-weight: 600;
    color: #1F2937;
}
QLabel#fileInfoPath {
    font-size: 12px;
    color: #9CA3AF;
}
QLabel#fileInfoMeta {
    font-size: 12px;
    color: #4B5563;
}

/* ------------------------------ 流程步骤 ------------------------------ */
StepChip {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 12px;
    padding: 4px 12px;
    font-size: 12px;
    color: #4B5563;
}
StepChip[stepState="active"] {
    background: #EFF6FF;
    border-color: #93C5FD;
    color: #1D4ED8;
    font-weight: 600;
}
StepChip[stepState="done"] {
    background: #ECFDF5;
    border-color: #A7F3D0;
    color: #047857;
}
StepChip[stepState="disabled"] {
    background: #F3F4F6;
    border-color: #E5E7EB;
    color: #9CA3AF;
}
QLabel#stepArrow {
    color: #CBD5E1;
    font-size: 12px;
}

/* ------------------------------ 进度条 ------------------------------ */
QProgressBar {
    background: #E5E7EB;
    border: none;
    border-radius: 6px;
    min-height: 10px;
    max-height: 10px;
    text-align: center;
    color: transparent;
}
QProgressBar::chunk {
    background: #2563EB;
    border-radius: 6px;
}

/* ------------------------------ 日志框 ------------------------------ */
QPlainTextEdit#logBox {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 8px;
    padding: 8px;
    font-family: "Consolas", "Microsoft YaHei UI", monospace;
    font-size: 12px;
    color: #334155;
}

/* ------------------------------ 滚动条 ------------------------------ */
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: #D1D5DB;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #9CA3AF;
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
    background: #D1D5DB;
    border-radius: 5px;
    min-width: 30px;
}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {
    width: 0;
}
"""
