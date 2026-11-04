"""应用内自动更新：Velopack + GitHub Releases 更新源。

核心模块（不依赖 Qt）：仅封装同步的更新管理器构造。检查 / 下载 /
应用的线程调度与 UI 交互由 app/ui/updater_flow.py 负责。

非 Velopack 安装环境（开发态、-SkipInstaller 裸 onedir）下
UpdateManager 构造抛 RuntimeError（Could not auto-locate app
manifest），create_update_manager() 捕获后返回 None，调用方据此
优雅降级：启动检查静默跳过，手动检查提示不支持。

更新源二选一（由设置键 update_mirror 决定，构造时读取——切换开关
后无需重启即生效）：
  - 默认：GithubSource（走 GitHub Releases API，需要能访问
    api.github.com / github.com）；
  - 镜像：HttpSource（静态文件源）指向 gh-proxy.com 公益镜像的
    releases/latest/download/ 目录。GithubSource 不能直接加镜像
    前缀——它按 owner/repo 调用 GitHub API，会把镜像主机误解析成
    GitHub Enterprise；HttpSource 则按"基址 + 文件名"取 feed 与
    nupkg，全部流量经镜像（gh-proxy 为 Cloudflare Worker，服务端
    跟随 GitHub 302 后流式回传），增量更新机制不受影响。
"""

from __future__ import annotations

import logging

from app.core.i18n import get_setting

logger = logging.getLogger(__name__)

# 更新源：GitHub Releases（公开仓库，无需 token；vpk upload 发布的
# releases.win.json feed 与全量 / 增量 nupkg 均托管于此）
UPDATE_REPO_URL = "https://github.com/yangjinduo/E-SubDub"

# 国内加速镜像（HttpSource 基址）：releases/latest/download/ 为 GitHub
# 官方的"最新 Release 资产"稳定路径，302 到具体 tag 后由镜像服务端跟随
UPDATE_MIRROR_BASE = (
    "https://gh-proxy.com/https://github.com/yangjinduo/E-SubDub"
    "/releases/latest/download/"
)


def create_update_manager():
    """构造更新管理器（按设置选择官方源 / 国内镜像）。

    返回 None 表示当前环境不支持自动更新（非 Velopack 安装），
    调用方不得抛出异常。
    """
    try:
        from velopack import GithubSource, HttpSource, UpdateManager

        if get_setting("update_mirror", False):
            source = HttpSource(UPDATE_MIRROR_BASE)
        else:
            source = GithubSource(UPDATE_REPO_URL)
        return UpdateManager(source)
    except Exception as exc:  # 非 Velopack 布局 / velopack 库缺失
        logger.info("自动更新不可用（非 Velopack 安装环境）：%s", exc)
        return None
