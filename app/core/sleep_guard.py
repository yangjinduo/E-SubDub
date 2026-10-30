"""系统休眠守卫：任务 / 模型下载 / 环境搭建进行期间阻止系统休眠。

用户可在设置窗口开关（持久化于 tasks.json 顶层 prevent_sleep 键，默认
开启）；开启后，应用在有活动（任务队列存在未终结任务、模型下载或环境
搭建进行中）时按下平台机制阻止休眠，活动结束自动解除：

- Windows：SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)。
  仅阻止系统休眠，屏幕仍按电源计划自动关闭；清除时恢复 ES_CONTINUOUS。
  执行状态绑定在调用线程（GUI 主线程，存活于整个应用生命周期）。
- macOS：常驻 caffeinate -i 子进程（阻止系统空闲休眠，不阻止屏幕
  关闭），活动结束时 terminate；caffeinate 缺失时降级为不阻止并记录
  warning。进程退出时经 atexit 兜底结束子进程。
- 其余平台：无操作。

所有公开函数应在主线程调用（set_active 的调用点均为 Qt 信号槽）。
"""

from __future__ import annotations

import atexit
import logging
import subprocess
import sys

from app.core.i18n import get_setting, set_setting

logger = logging.getLogger(__name__)

_SETTING_KEY = "prevent_sleep"

# SetThreadExecutionState 标志位
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001

_enabled = False     # 用户设置：是否启用防休眠
_active = False      # 当前是否有活动（任务 / 下载 / 环境搭建）
_loaded = False      # 是否已从持久化配置加载
_applied = False     # 平台机制当前是否处于“阻止休眠”状态
_caffeinate: subprocess.Popen | None = None  # macOS：caffeinate 子进程


def _ensure_loaded() -> None:
    """首次访问时从 tasks.json 读取持久化的开关状态。"""
    global _enabled, _loaded
    if _loaded:
        return
    _loaded = True
    _enabled = bool(get_setting(_SETTING_KEY, True))


def is_enabled() -> bool:
    """用户是否启用了「运行期间禁止系统休眠」。"""
    _ensure_loaded()
    return _enabled


def set_enabled(enabled: bool) -> None:
    """开关防休眠（立即持久化），并按当前活动状态立即应用/解除。"""
    global _enabled
    _ensure_loaded()
    enabled = bool(enabled)
    if enabled == _enabled:
        return
    _enabled = enabled
    set_setting(_SETTING_KEY, enabled)
    _sync()
    logger.info("防休眠已%s", "开启" if enabled else "关闭")


def set_active(active: bool) -> None:
    """更新「是否有活动」状态（任务 / 下载 / 环境搭建任一进行中）。"""
    global _active
    _ensure_loaded()
    active = bool(active)
    if active == _active:
        return
    _active = active
    _sync()
    logger.debug(
        "防休眠活动状态：%s（启用=%s）", "有活动" if active else "无活动", _enabled,
    )


def _sync() -> None:
    """按（启用 且 有活动）同步平台防休眠状态。"""
    global _applied
    should = _enabled and _active
    if should == _applied:
        return
    try:
        if should:
            _apply()
        else:
            _release()
    except Exception as exc:  # noqa: BLE001 - 防休眠失败不应影响任务流程
        logger.warning("同步防休眠状态失败：%s", exc)
        return
    _applied = should
    logger.info("系统休眠阻止已%s", "生效" if should else "解除")


# ------------------------------------------------------------------ 平台实现

def _apply() -> None:
    """按下平台机制开始阻止系统休眠。"""
    global _caffeinate
    if sys.platform == "win32":
        import ctypes

        if ctypes.windll.kernel32.SetThreadExecutionState(
            _ES_CONTINUOUS | _ES_SYSTEM_REQUIRED,
        ) == 0:
            raise RuntimeError("SetThreadExecutionState 调用失败")
    elif sys.platform == "darwin":
        try:
            _caffeinate = subprocess.Popen(
                ["caffeinate", "-i"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:
            raise RuntimeError(f"启动 caffeinate 失败：{exc}") from exc
    # 其余平台：无操作


def _release() -> None:
    """解除系统休眠阻止。"""
    global _caffeinate
    if sys.platform == "win32":
        import ctypes

        ctypes.windll.kernel32.SetThreadExecutionState(_ES_CONTINUOUS)
    elif sys.platform == "darwin":
        if _caffeinate is not None:
            _caffeinate.terminate()
            try:
                _caffeinate.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _caffeinate.kill()
            _caffeinate = None


@atexit.register
def _cleanup() -> None:
    """进程退出兜底：解除执行状态 / 结束 caffeinate 子进程。"""
    global _applied
    if not _applied:
        return
    _applied = False
    try:
        _release()
    except Exception:  # noqa: BLE001 - 退出清理不抛出
        pass
