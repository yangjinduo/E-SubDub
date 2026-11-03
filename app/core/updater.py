"""应用内自动更新：Velopack + GitHub Releases 更新源。

核心模块（不依赖 Qt）：仅封装同步的更新管理器构造。检查 / 下载 /
应用的线程调度与 UI 交互由 app/ui/updater_flow.py 负责。

非 Velopack 安装环境（开发态、-SkipInstaller 裸 onedir）下
UpdateManager 构造抛 RuntimeError（Could not auto-locate app
manifest），create_update_manager() 捕获后返回 None，调用方据此
优雅降级：启动检查静默跳过，手动检查提示不支持。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 更新源：GitHub Releases（公开仓库，无需 token；vpk upload 发布的
# releases.win.json feed 与全量 / 增量 nupkg 均托管于此）
UPDATE_REPO_URL = "https://github.com/yangjinduo/E-SubDub"


def create_update_manager():
    """构造指向 GitHub Releases 的 UpdateManager。

    返回 None 表示当前环境不支持自动更新（非 Velopack 安装），
    调用方不得抛出异常。
    """
    try:
        from velopack import GithubSource, UpdateManager

        return UpdateManager(GithubSource(UPDATE_REPO_URL))
    except Exception as exc:  # 非 Velopack 布局 / velopack 库缺失
        logger.info("自动更新不可用（非 Velopack 安装环境）：%s", exc)
        return None
