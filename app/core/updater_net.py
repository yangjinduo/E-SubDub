"""自建更新传输层：requests 拉取 feed 与更新包（绕过 velopack 网络栈）。

为什么不用 velopack 自带的下载：其 Rust HTTP 客户端（ureq）不读取
Windows 系统代理，且默认整个请求无超时——国内代理网络下两类死局
均由此而来：官方源 TLS 直连被劫持（invalid peer certificate: UnknownIssuer）、
镜像源能检查但 nupkg 下载无限挂起（进度永远 0%，无任何报错）。
requests 默认读取系统代理（浏览器同栈），connect/read 超时按 socket
真实生效（读超时 = 数据流停滞 30 秒即报错，彻底消灭"永远 0%"），
流式下载可随时取消。

职责（全部同步阻塞，调用方负责放后台线程，见 app/ui/updater_flow.py）：
- fetch_feed()：拉取 releases.win.json，官方 / 镜像按设置优先，
  一端失败自动换另一端；
- pick_full_asset()：选出 feed 中版本最高的 Full nupkg 条目；
- pick_delta_asset()：选与目标 Full 同版本的 Delta nupkg 条目
  （有则启用增量：只下载几 MB 的补丁，由 velopack 在本机用
  packages 目录里的基线 full 包重建目标 full 包；无则走全量）；
- download_nupkg()：流式下载 + 实时进度 + 取消 + SHA256 校验；
- build_local_feed()：把选中条目写为本地 feed，供本地目录源的
  UpdateManager 消费（check / download / apply 全走本地文件，
  不再碰网络）。增量路径写入 Full+Delta 两条目——velopack 的
  策略：feed 中存在与目标版本同 Version 的 Delta 条目即启用
  增量，基线取自本机 packages 目录已安装的 full 包，补丁重建
  由 Update.exe patch 完成，重建失败自动回退从源下载 full。
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path

import requests

from app.core.i18n import get_setting

logger = logging.getLogger(__name__)

# GitHub Releases 官方与 gh-proxy 镜像的"最新 Release 资产"基址
# （releases/latest/download/ 为 GitHub 官方稳定路径，302 到具体 tag）
OFFICIAL_BASE = (
    "https://github.com/yangjinduo/E-SubDub/releases/latest/download/"
)
MIRROR_BASE = (
    "https://gh-proxy.com/https://github.com/yangjinduo/E-SubDub"
    "/releases/latest/download/"
)

# feed 文件名（vpk upload 产出的 velopack feed；RID=win）
FEED_NAME = "releases.win.json"

# requests 超时：(连接, 读)。读超时作用于相邻数据块之间——链路停滞
# 超过该秒数立即报错
CONNECT_TIMEOUT = 10
READ_TIMEOUT = 30


class UpdateNetError(RuntimeError):
    """更新传输层错误（feed 拉取 / 下载 / 校验失败）。"""


class UpdateCancelled(Exception):
    """用户取消下载。"""


def update_cache_dir() -> Path:
    """下载缓存目录（%LOCALAPPDATA%\\ESubDub\\update-cache）。

    固定目录反复复用，下载前由调用方清空，不产生孤儿临时目录。
    """
    from platformdirs import user_config_dir

    d = Path(user_config_dir("ESubDub", appauthor=False)) / "update-cache"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _version_key(version: str) -> tuple:
    """版本号 → 可比较元组（"0.1.3" → (0, 1, 3)；解析失败按 (0,)）。"""
    nums = re.findall(r"\d+", str(version))
    return tuple(int(n) for n in nums) or (0,)


def fetch_feed() -> tuple[dict, str]:
    """拉取 feed，返回 (feed_json, 下载基址)。

    按设置 update_mirror 决定先试官方还是镜像，一端失败自动换
    另一端；两端都失败抛 UpdateNetError（调用方回退 velopack 原生
    流程，见 app/ui/updater_flow.py）。
    """
    bases = (
        [MIRROR_BASE, OFFICIAL_BASE]
        if get_setting("update_mirror", False)
        else [OFFICIAL_BASE, MIRROR_BASE]
    )
    errors: list[str] = []
    for base in bases:
        try:
            resp = requests.get(
                base + FEED_NAME, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
            )
            resp.raise_for_status()
            feed = json.loads(resp.content.decode("utf-8"))
            if not feed.get("Assets"):
                raise ValueError("feed 无 Assets 条目")
            logger.info(
                "更新 feed 拉取成功：%s（%d 个资产条目）",
                base, len(feed["Assets"]),
            )
            return feed, base
        except Exception as exc:
            logger.warning("更新 feed 拉取失败（%s）：%s", base, exc)
            errors.append(f"{base}: {exc}")
    raise UpdateNetError("；".join(errors))


def pick_full_asset(feed: dict) -> dict | None:
    """feed 中版本最高的 Full nupkg 条目（无 Full 条目返回 None）。"""
    fulls = [a for a in feed.get("Assets", []) if a.get("Type") == "Full"]
    if not fulls:
        return None
    return max(fulls, key=lambda a: _version_key(a.get("Version", "")))


def pick_delta_asset(feed: dict, full_asset: dict) -> dict | None:
    """与目标 Full 同版本的 Delta nupkg 条目（无则返回 None）。

    velopack（vpk pack）只为相邻版本生成 delta，且 GitHub
    releases/latest 的 feed 只含最新版本条目——因此只有"用户
    已装上一版、最新版正好有 delta"时才有增量机会，其余情况
    返回 None 走全量，不影响正确性。
    """
    target = str(full_asset.get("Version") or "")
    if not target:
        return None
    for a in feed.get("Assets", []):
        if a.get("Type") == "Delta" and str(a.get("Version")) == target:
            return a
    return None


def is_newer(asset: dict, current_version: str) -> bool:
    """feed 条目版本是否高于本地已安装版本。"""
    return _version_key(asset.get("Version", "")) > _version_key(current_version)


def download_nupkg(
    base: str,
    asset: dict,
    dest_dir: Path,
    progress=None,
    cancelled=None,
) -> Path:
    """流式下载 Full nupkg 到 dest_dir，返回落盘路径。

    progress：进度回调（0-100 整数）；cancelled：取消检查回调
    （返回 True 时中止并抛 UpdateCancelled）。下载完成做 SHA256
    校验（与 feed 声明的摘要比对），不匹配抛 UpdateNetError。
    """
    file_name = str(asset.get("FileName") or "")
    if not file_name:
        raise UpdateNetError("feed 条目缺少文件名")
    url = base + file_name
    dest = dest_dir / file_name
    expected_sha = str(asset.get("SHA256") or "").strip().lower()

    logger.info("开始下载更新包：%s", url)
    with requests.get(
        url, stream=True, timeout=(CONNECT_TIMEOUT, READ_TIMEOUT)
    ) as resp:
        resp.raise_for_status()
        total = int(resp.headers.get("Content-Length") or asset.get("Size") or 0)
        done = 0
        last_pct = -1
        sha = hashlib.sha256()
        tmp = dest.with_name(dest.name + ".part")
        with open(tmp, "wb") as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if cancelled is not None and cancelled():
                    f.close()
                    tmp.unlink(missing_ok=True)
                    raise UpdateCancelled()
                if not chunk:
                    continue
                f.write(chunk)
                sha.update(chunk)
                done += len(chunk)
                if progress is not None and total > 0:
                    pct = min(99, int(done * 100 / total))
                    if pct > last_pct:
                        last_pct = pct
                        progress(pct)
        if expected_sha and sha.hexdigest().lower() != expected_sha:
            tmp.unlink(missing_ok=True)
            raise UpdateNetError("更新包校验失败（SHA256 不匹配）")
        tmp.replace(dest)
    if progress is not None:
        progress(100)
    logger.info("更新包下载完成：%s（%d 字节）", dest, done)
    return dest


def build_local_feed(dest_dir: Path, assets: list[dict]) -> Path:
    """把选中的条目写为本地 feed（releases.win.json）。

    只写入已下载到位的条目（增量路径 = Full+Delta 两条，全量路径
    = Full 一条），保证本地源的 UpdateManager 不会试图拉取未下载
    的条目；Full 条目必须始终在场——它是 velopack 增量重建失败时
    的回退目标。
    """
    feed_path = dest_dir / FEED_NAME
    feed_path.write_text(
        json.dumps({"Assets": assets}, ensure_ascii=False), encoding="utf-8"
    )
    return feed_path
