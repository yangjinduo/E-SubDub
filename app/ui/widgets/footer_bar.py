"""主窗口底部页脚：左侧声音克隆合规提示（自动换行）+ 右侧意见反馈邮箱。

纯静态展示控件，无业务逻辑；语言切换时随 MainWindow._build_ui() 整体
重建，构造期取 tr() 即可跟随界面语言。左侧提示与声音克隆授权对话框
（task_dialog.py）复用同一键 main.voice_clone_text。
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel

from app.core.i18n import tr


class FooterBar(QFrame):
    """底部页脚栏：左侧合规提示按剩余宽度自动换行，右侧邮箱可选中复制。"""

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        # 左：合规提示（wordWrap 随窗口宽度自动换行，占满余宽）
        self._notice_label = QLabel(tr("main.voice_clone_text"))
        self._notice_label.setObjectName("footerText")
        self._notice_label.setWordWrap(True)

        # 右：意见反馈（文本可选中，方便复制邮箱）
        self._feedback_label = QLabel(tr("main.feedback"))
        self._feedback_label.setObjectName("footerFeedback")
        self._feedback_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        layout = QHBoxLayout(self)
        # 无外层卡片，留白交给主窗口布局统一控制（与 SystemMonitorBar 一致）
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._notice_label, 1)
        layout.addWidget(self._feedback_label, 0, Qt.AlignmentFlag.AlignVCenter)
