"""应用内自动更新：Velopack + GitHub Releases 更新源。

核心模块（不依赖 Qt）：velopack 更新管理器构造（官方 / 镜像 / 本地
目录源）。网络传输由 app/core/updater_net.py（requests 自建传输层）
承担，检查 / 下载 / 应用的线程调度与 UI 交互由
app/ui/updater_flow.py 负责。

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
    镜像源必须带 HttpOptions 超时：velopack 默认 0 = 整个请求
    永不超时，而其 Rust HTTP 客户端（ureq）又不读 Windows 系统代理
    ——这正是国内网络"下载永远 0%"死局的两大帮凶（首选的 requests
    自建传输层已根治，此处仅给回退路径兜底）。

本地目录源（create_local_update_manager）：自建传输层把 feed 与
Full nupkg 下载到缓存目录后，UpdateManager 以目录路径字符串构造
（stub：source 接受"URL/路径"自动探测），check / download（本地
拷贝）/ apply 全走本地文件，全程不碰网络。
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

# 镜像源整请求超时（连接 + 传输总量，需覆盖 130MB 级 nupkg 的慢速
# 下载）；链路停滞（无数据）由 UI 层看门狗更早中止，见 updater_flow
MIRROR_TIMEOUT_MS = 600_000


def create_update_manager():
    """构造更新管理器（按设置选择官方源 / 国内镜像）。

    返回 None 表示当前环境不支持自动更新（非 Velopack 安装），
    调用方不得抛出异常。
    """
    try:
        from velopack import (
            GithubSource,
            HttpOptions,
            HttpSource,
            UpdateManager,
        )

        if get_setting("update_mirror", False):
            # HttpOptions 只有 Headers / TimeoutMilliseconds 两个旋钮
            # （无代理选项）——超时是能设的全部，但已足够消灭"永不
            # 超时的永久挂起"
            source = HttpSource(
                UPDATE_MIRROR_BASE, HttpOptions([], MIRROR_TIMEOUT_MS)
            )
        else:
            # GithubSource 的 Python API 不接受 HttpOptions（仅
            # repo_url / access_token / prerelease），官方源超时只能
            # 靠 UI 层看门狗兜底
            source = GithubSource(UPDATE_REPO_URL)
        return UpdateManager(source)
    except Exception as exc:  # 非 Velopack 布局 / velopack 库缺失
        logger.info("自动更新不可用（非 Velopack 安装环境）：%s", exc)
        return None


def create_local_update_manager(feed_dir):
    """本地目录源的更新管理器。

    feed（单条目 releases.win.json）与 Full nupkg 已由自建传输层
    下载到 feed_dir；UpdateManager 以路径字符串构造后，check /
    download（本地拷贝）/ apply 全走本地文件，不碰网络。
    """
    from velopack import UpdateManager

    return UpdateManager(str(feed_dir))
