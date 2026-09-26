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
/* 已完成的流程步骤：可点击，悬停高亮提示可查看结果 */
StepChip[stepState="done"]:hover {
    background: #D1FAE5;
    border-color: #34D399;
    color: #065F46;
}
QLabel#stepArrow {
    color: #CBD5E1;
    font-size: 12px;
}

/* ------------------------------ 任务列表（主窗口） ------------------------------ */
QTableWidget#taskTable {
    background: #FFFFFF;
    border: 1px solid #E5E7EB;
    border-radius: 10px;
    gridline-color: #F3F4F6;
    selection-background-color: #EFF6FF;
    selection-color: #1D4ED8;
}
QTableWidget#taskTable QHeaderView::section {
    background: #F9FAFB;
    color: #4B5563;
    border: none;
    border-bottom: 1px solid #E5E7EB;
    padding: 8px 6px;
    font-weight: 600;
}

/* 新建任务按钮（任务列表头部） */
QPushButton#addTaskButton {
    background: #2563EB;
    border: 1px solid #2563EB;
    border-radius: 8px;
    padding: 6px 18px;
    color: #FFFFFF;
    font-weight: 600;
}
QPushButton#addTaskButton:hover {
    background: #1D4ED8;
    border-color: #1D4ED8;
}
QPushButton#addTaskButton:pressed {
    background: #1E40AF;
}

/* ------------------------------ 系统监视栏（深色格 + 折线图） ------------------------------ */
QFrame#monitorCell {
    background: #111827;
    border: 1px solid #1F2937;
    border-radius: 10px;
}
QLabel#monitorName {
    color: #9CA3AF;
    font-size: 12px;
}
QLabel#monitorValue {
    color: #F9FAFB;
    font-size: 15px;
    font-weight: 600;
}
QLabel#monitorSub {
    color: #6B7280;
    font-size: 11px;
}
QFrame#sparkline {
    background: transparent;
}

/* ------------------------------ 流程结果查看窗口 ------------------------------ */
QLabel#viewerPathLabel {
    color: #6B7280;
    font-size: 12px;
}
QLabel#viewerHint {
    color: #6B7280;
}
QLabel#viewerTrackTitle {
    font-size: 13px;
    font-weight: 600;
    color: #111827;
}
QLabel#viewerFallback {
    color: #6B7280;
    font-size: 13px;
    padding: 16px;
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
