"""应用入口：创建 QApplication、应用全局样式并显示主窗口。"""

import os
import sys
from pathlib import Path

# 允许以 `python app/main.py` 方式直接启动：把项目根加入 sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication

from app.core.config import AppConfig
from app.core.hardware import detect_device_standalone
from app.ui.main_window import MainWindow
from app.ui.style import APPLICATION_QSS


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyleSheet(APPLICATION_QSS)

    # 应用 LOGO（app/resources/logo）：作用于全部窗口标题栏与任务栏图标
    icon_path = AppConfig.app_icon_path()
    if icon_path:
        app.setWindowIcon(QIcon(icon_path))

    # 启动早期用不依赖 torch 的粗检测快速显示设备状态
    device = detect_device_standalone()
    window = MainWindow(device_info=device)
    window.show()
    return app.exec()


if __name__ == "__main__":
    code = main()
    # 直接退出进程：极端情况下（退出时仍有流程线程卡在长耗时操作中）
    # 解释器收尾回收会销毁运行中的 QThread 导致崩溃；跳过收尾直接退出
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
