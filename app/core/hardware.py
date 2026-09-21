"""硬件与推理设备检测。

提供两种检测方式：
- detect_device(): 基于 torch 的精确检测（torch 延迟导入，缺失时回退 CPU）
- detect_device_standalone(): 不依赖 torch 的粗粒度检测，供应用启动早期快速显示
"""

import platform
import sys
from dataclasses import dataclass

from app.core.i18n import tr


@dataclass(frozen=True)
class DeviceInfo:
    """推理设备信息。

    属性：
        device: 设备类型，"cuda" / "mps" / "cpu"
        is_slow: 是否为慢速设备（CPU 视为慢速）
        name: 显卡名称（仅 CUDA 有值），用于展示
        description: 人类可读描述（按当前界面语言动态生成）
    """

    device: str
    is_slow: bool
    name: str = ""

    @property
    def description(self) -> str:
        """按当前界面语言生成描述，语言切换后无需重新检测。"""
        if self.device == "cuda":
            return tr("device.cuda", name=self.name or "GPU")
        if self.device == "mps":
            return tr("device.mps")
        return tr("device.cpu")


# 慢速 CPU 的统一描述
_CPU_SLOW = DeviceInfo(device="cpu", is_slow=True)

# Windows 显示适配器设备类的注册表路径（枚举显卡 DriverDesc 用）
_WIN_GPU_CLASS_KEY = r"SYSTEM\CurrentControlSet\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}"


def detect_device() -> DeviceInfo:
    """基于 torch 的精确设备检测。

    优先级：NVIDIA CUDA > macOS MPS > CPU（慢速）。
    torch 延迟导入，任何导入或查询异常均安全回退到 CPU。
    """
    try:
        import torch  # 延迟导入：仅在调用时加载
    except Exception:
        # torch 未安装或导入失败，回退 CPU
        return _CPU_SLOW

    try:
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            return DeviceInfo(device="cuda", is_slow=False, name=name)

        mps = getattr(torch.backends, "mps", None)
        if (
            sys.platform == "darwin"
            and platform.machine() == "arm64"
            and mps is not None
            and mps.is_available()
        ):
            return DeviceInfo(device="mps", is_slow=False)
    except Exception:
        # 检测过程中出现异常，保守回退 CPU
        return _CPU_SLOW

    return _CPU_SLOW


def _iter_windows_gpu_names():
    """遍历 Windows 注册表中显示适配器的 DriverDesc 描述，找不到时返回空列表。"""
    import winreg  # 局部导入：仅 Windows 存在

    names = []
    try:
        base = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _WIN_GPU_CLASS_KEY)
    except OSError:
        return names

    with base:
        index = 0
        while True:
            try:
                sub = winreg.OpenKey(base, f"{index:04d}")
            except OSError:
                # 子键枚举完毕
                break
            with sub:
                try:
                    desc, _ = winreg.QueryValueEx(sub, "DriverDesc")
                    names.append(str(desc))
                except OSError:
                    pass
            index += 1
    return names


def detect_device_standalone() -> DeviceInfo:
    """不依赖 torch 的粗粒度设备检测（启动早期快速显示用）。

    - Windows: 查注册表判断是否存在 NVIDIA 显卡
    - macOS: 按 platform.machine() 判断 Apple Silicon / Intel
    - 其余: 回退 CPU（慢速）
    """
    if sys.platform == "win32":
        for name in _iter_windows_gpu_names():
            if "nvidia" in name.lower():
                return DeviceInfo(device="cuda", is_slow=False, name=name)
        return _CPU_SLOW

    if sys.platform == "darwin":
        if platform.machine() == "arm64":
            return DeviceInfo(device="mps", is_slow=False)
        return _CPU_SLOW

    return _CPU_SLOW
