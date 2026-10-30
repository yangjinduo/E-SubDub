"""应用入口：创建 QApplication、应用全局样式并显示主窗口。

冻结态（Nuitka 安装包）禁用了控制台，启动期任何异常都是"无声死"：
双击无反应、看不到任何报错。为此本模块在最早阶段把日志接到用户
配置目录的启动日志（文件名带日期，如 startup2026-10-16.log，与
tasks.json 同目录，见 task_store.py 的路径说明），并把全部启动步骤
（含模块导入）包进 try/except：崩溃时写日志并尽力弹出错误框，保证
异常路径可诊断。

开发态（python app/main.py）行为不变，仅额外写同一套启动日志。
"""

import faulthandler
import logging
import os
import sys
import time
import traceback
from pathlib import Path

# 允许以 `python app/main.py` 方式直接启动：把项目根加入 sys.path
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _rotate_old_logs(log_dir: Path, current: Path) -> None:
    """清理旧启动日志：最多保留 2 份（当前这份 + 最近的一份旧日志）。

    - 按文件名排序即按日期排序（startup2026-10-16.log 的字典序与
      日期序一致）；旧版遗留的无日期 startup.log 排在最后，会被
      优先清掉；
    - 清理失败（文件被占用等）不影响启动。
    """
    try:
        old_logs = sorted(
            (p for p in log_dir.glob("startup*.log") if p != current),
            key=lambda p: p.name,
            reverse=True,
        )
        for stale in old_logs[1:]:  # 留 1 份旧日志，更早的全部删除
            stale.unlink(missing_ok=True)
    except Exception:
        pass


def _init_startup_log() -> Path | None:
    """在最早阶段接入启动日志（用户配置目录的 startup日期.log）。

    - 目录与 tasks.json 一致（platformdirs + appauthor=False：Windows
      %LOCALAPPDATA%\\ESubDub\\，macOS ~/Library/Application
      Support/ESubDub/），冻结态 / 开发态路径相同；
    - 文件名带当天日期（如 startup2026-10-16.log）：同一天多次启动
      追加写入同一份，跨天自动切换新文件；
    - 每次启动清理旧日志，磁盘上最多保留 2 份（见 _rotate_old_logs）；
    - 日志系统自身故障（目录不可写等）绝不阻塞应用启动，返回 None。
    """
    try:
        from platformdirs import user_config_dir

        log_dir = Path(user_config_dir("ESubDub", appauthor=False))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"startup{time.strftime('%Y-%m-%d')}.log"
        _rotate_old_logs(log_dir, log_path)
        # 同一天追加（跨天自然落到新文件），日志里的"=== 启动 ==="
        # 行就是一天内多次启动的分隔线
        handler = logging.FileHandler(log_path, mode="a", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        root = logging.getLogger()
        root.setLevel(logging.INFO)
        root.addHandler(handler)
        return log_path
    except Exception:
        return None


def _crash_dialog(log_path: Path | None, exc_text: str) -> None:
    """尽力弹出启动失败错误框（冻结态无控制台，这是唯一可见线索）。"""
    try:
        from PySide6.QtWidgets import QApplication, QMessageBox

        if QApplication.instance() is None:
            QApplication(sys.argv)
        tip = f"\n详细日志：{log_path}" if log_path else ""
        QMessageBox.critical(
            None,
            "E-SubDub 启动失败",
            "启动过程中发生错误，程序即将退出。"
            f"{tip}\n\n{exc_text[-1200:]}",
        )
    except Exception:
        # 错误框失败也不吞掉原异常（堆栈已写入启动日志）
        pass


def main() -> int:
    log_path = _init_startup_log()
    logging.info("=== 启动 ===（exe=%s，argv=%s）", sys.executable, sys.argv)
    # 原生层崩溃（非法内存访问等）不是 Python 异常，try/except 抓不到：
    # faulthandler 注册系统级处理器，崩溃瞬间把全部线程栈写进日志。
    # 另设 20 秒看门狗：启动若卡死（无异常也无进展），自动倾倒全部
    # 线程栈暴露阻塞行；窗口正常显示后取消（崩溃捕获全程保持注册）。
    if log_path is not None:
        try:
            fh = open(log_path, "a", encoding="utf-8")
            faulthandler.enable(file=fh, all_threads=True)
            faulthandler.dump_traceback_later(20, file=fh, exit=False)
        except Exception:
            pass
    try:
        # 导入全部放在 try 内：模块级导入失败同样要写日志、弹窗
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication

        from app.core.config import AppConfig, is_frozen
        from app.core.hardware import detect_device_standalone
        from app.ui.main_window import MainWindow
        from app.ui.style import apply_theme

        logging.info("模块导入完成（frozen=%s）", is_frozen())
        app = QApplication(sys.argv)
        logging.info("QApplication 已创建")
        # 应用当前主题（默认深色）：Fusion 样式 + 显式调色板 + 主题 QSS，
        # 不受 Windows 10/11 系统深浅色模式影响
        apply_theme(app)

        # 应用 LOGO（app/resources/logo）：作用于全部窗口标题栏与任务栏图标
        icon_path = AppConfig.app_icon_path()
        if icon_path:
            app.setWindowIcon(QIcon(icon_path))
        logging.info("LOGO 已加载：%s", icon_path)

        # 启动早期用不依赖 torch 的粗检测快速显示设备状态
        device = detect_device_standalone()
        logging.info("设备检测完成：%s", device)
        window = MainWindow(device_info=device)
        logging.info("主窗口构造完成")
        window.show()
        # 窗口已正常显示：取消启动卡死看门狗，避免运行 20 秒后
        # 误倾倒线程栈（faulthandler 的原生崩溃捕获保持注册，全程有效）
        faulthandler.cancel_dump_traceback_later()
        logging.info("窗口已显示，进入事件循环")
        return app.exec()
    except Exception:
        exc_text = traceback.format_exc()
        logging.error("启动失败：\n%s", exc_text)
        _crash_dialog(log_path, exc_text)
        return 1


if __name__ == "__main__":
    code = main()
    # 直接退出进程：极端情况下（退出时仍有流程线程卡在长耗时操作中）
    # 解释器收尾回收会销毁运行中的 QThread 导致崩溃；跳过收尾直接退出
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
