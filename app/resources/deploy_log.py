# -*- coding: utf-8 -*-
"""部署日志：运行环境搭建与模型下载全过程的文件日志。

写入用户配置目录（与 startup 日志同目录，Windows
%LOCALAPPDATA%/ESubDub/、macOS ~/Library/Application Support/ESubDub/），
文件名 deployYYYY-MM-DD.log（同日追加，跨天换新文件），最多保留
2 份（当前 + 最近 1 份旧日志，清理逻辑与 startup 日志一致）。

记录内容：搭建 / 下载的每一步、完整命令行、pip 等子进程的全部
输出行、下载进度与重试、失败的完整堆栈。搭建或下载出错时错误
信息会附上日志文件路径——冻结态（Nuitka 安装包）无控制台，这是
部署问题唯一的过程级诊断入口（startup 日志只记录启动里程碑与
应用级异常，不放子进程输出，避免互相污染）。

环境搭建线程与多个下载线程并发写同一份文件：logging 的
FileHandler 按记录原子写入（内部锁），行级交错但不撕裂。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

from platformdirs import user_config_dir

from app.core.i18n import APP_NAME

_LOGGER_NAME = "esubdub.deploy"

_logger: logging.Logger | None = None
_log_path: Path | None = None


def _init() -> bool:
    """创建当天的 deploy 日志并挂到专用 logger（失败静默返回 False）。

    日志系统自身故障（配置目录不可写等）绝不阻塞搭建 / 下载流程；
    初始化失败时后续调用会再次尝试（无害）。
    """
    global _logger, _log_path
    try:
        log_dir = Path(user_config_dir(APP_NAME, appauthor=False))
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"deploy{time.strftime('%Y-%m-%d')}.log"
        # 旧日志清理：文件名含日期，字典序即时间序；保留最近 1 份旧日志
        old_logs = sorted(
            (p for p in log_dir.glob("deploy*.log") if p != path),
            key=lambda p: p.name,
            reverse=True,
        )
        for stale in old_logs[1:]:
            stale.unlink(missing_ok=True)
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        )
        lg = logging.getLogger(_LOGGER_NAME)
        lg.setLevel(logging.INFO)
        # 不并入 root（startup 日志）：pip 输出行数大，两类日志各司其职
        lg.propagate = False
        lg.addHandler(handler)
        _logger, _log_path = lg, path
        return True
    except Exception:
        return False


def deploy_logger() -> logging.Logger:
    """部署日志 logger（首次调用时初始化文件；初始化失败时静默丢弃）。"""
    if _logger is None:
        _init()
    return _logger if _logger is not None else logging.getLogger(_LOGGER_NAME)


def deploy_log_path() -> Path | None:
    """部署日志文件路径（尚未初始化或初始化失败时为 None）。"""
    if _log_path is None:
        _init()
    return _log_path
