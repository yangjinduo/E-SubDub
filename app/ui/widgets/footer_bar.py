"""主窗口底部页脚：左侧声音克隆合规提示（自动换行）+ 右侧意见反馈邮箱、GitHub 仓库与开源声明入口。

静态展示控件之外有两处交互：「GitHub 仓库」点击直接用系统浏览器打开项目主页；
「开源声明」发信号，由主窗口打开声明窗口。语言切换时随 MainWindow._build_ui()
整体重建，构造期取 tr() 即可跟随界面语言。左侧提示与声音克隆授权对话框
（task_dialog.py）复用同一键 main.voice_clone_text。
"""

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton

from app.core.i18n import tr

GITHUB_REPO_URL = "https://github.com/yangjinduo/E-SubDub"


class FooterBar(QFrame):
    """底部页脚栏：左侧合规提示按剩余宽度自动换行，右侧邮箱可选中复制。"""

    notice_requested = Signal()  # 点击「开源声明」：由主窗口打开声明窗口

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

        # 中右：GitHub 仓库（链接样式按钮，系统浏览器打开项目主页）
        self._github_button = QPushButton(tr("main.github_repo"))
        self._github_button.setObjectName("footerGithub")
        self._github_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._github_button.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl(GITHUB_REPO_URL))
        )

        # 最右：开源声明（链接样式按钮，打开 THIRD-PARTY-NOTICES 声明窗口）
        self._notice_button = QPushButton(tr("main.open_source_notice"))
        self._notice_button.setObjectName("footerNotice")
        self._notice_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
        self._notice_button.clicked.connect(lambda: self.notice_requested.emit())

        layout = QHBoxLayout(self)
        # 无外层卡片，留白交给主窗口布局统一控制（与 SystemMonitorBar 一致）
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        layout.addWidget(self._notice_label, 1)
        layout.addWidget(self._feedback_label, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._github_button, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addWidget(self._notice_button, 0, Qt.AlignmentFlag.AlignVCenter)
