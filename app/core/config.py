"""全局配置：应用信息（名称 / 版本）与目录路径推导。

目录布局（三种形态，见 _resolve_roots）：
- 冻结态 + Velopack 布局（正式安装）：APP_ROOT = <安装根>\\current\\
  （程序自身资源：app/resources、runtime/python 等），DATA_ROOT =
  <安装根>（models、tools、third_party、.venv-ml、.venv-tts 与
  current 同级）。Velopack 更新时整体替换 current，数据目录不受影响。
- 冻结态裸 onedir（-SkipInstaller 产物，本地测试）：APP_ROOT =
  DATA_ROOT = exe 所在目录，行为与旧版一致。
- 开发态（python app/main.py）：APP_ROOT = 项目根（等价于 current），
  DATA_ROOT = 项目根的上一级（等价于安装根，镜像冻结态布局）。

仅 tasks.json、startup 日志等轻量配置存于用户配置目录（Windows
%LOCALAPPDATA%/ESubDub/，macOS ~/Library/Application
Support/ESubDub/，见 i18n._config_dir）。
"""

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    """是否运行在 Nuitka 冻结可执行中（standalone/onedir）。

    Nuitka 编译的模块注入模块级 __compiled__ 常量，并设置 sys.frozen。
    """
    if bool(getattr(sys, "frozen", False)):
        return True
    module = sys.modules.get(__name__)
    return module is not None and "__compiled__" in dir(module)


def _is_velopack_layout(exe_dir: Path) -> bool:
    """冻结 exe 是否位于 Velopack 安装布局的 current 目录内。

    Velopack 安装根结构：<安装根>\\current\\（实际程序）+ Update.exe
    （更新器）。exe 所在目录名为 current 且父目录存在 Update.exe
    即认定为 Velopack 布局。
    """
    return exe_dir.name == "current" and (exe_dir.parent / "Update.exe").is_file()


def _resolve_roots() -> tuple[Path, Path]:
    """推导 (APP_ROOT, DATA_ROOT)。

    - 冻结态 + Velopack 布局：APP_ROOT = exe 所在目录（current，
      程序自身资源随包分发，更新时被整体替换）；DATA_ROOT = 其父目录
      （安装根）——模型 / 虚拟环境 / 工具 / 源码克隆等大体积可写数据
      与 current 同级，跨更新保留；
    - 冻结态裸 onedir（-SkipInstaller 产物）：APP_ROOT = DATA_ROOT =
      exe 所在目录，本地测试行为与旧版一致；
    - 开发态：APP_ROOT = 项目根（app 包上一级，等价于 current），
      DATA_ROOT = 项目根的上一级（等价于安装根，镜像冻结态布局）。
    """
    if is_frozen():
        exe_dir = Path(sys.executable).resolve().parent
        if _is_velopack_layout(exe_dir):
            return exe_dir, exe_dir.parent
        return exe_dir, exe_dir
    app_root = Path(__file__).resolve().parents[2]
    return app_root, app_root.parent


_APP_ROOT, _DATA_ROOT = _resolve_roots()


class AppConfig:
    """全局配置类。"""

    # 应用名称：E 表示 EASY（与中文“易”同音）；SubDub = Subtitle + Dubbing
    APP_NAME: str = "E-SubDub"

    # 应用版本（语义化版本）
    APP_VERSION: str = "0.1.6"

    # 应用根目录（程序自身资源所在，等价于 Velopack 的 current：
    # 开发态=项目根；冻结态=exe 所在目录，只读资源随包分发）
    APP_ROOT: Path = _APP_ROOT

    # 数据根目录（大体积可写数据，等价于 Velopack 的安装根）：
    # 冻结态+Velopack=exe 目录父级；冻结态裸 onedir=exe 目录；
    # 开发态=项目根上一级。模型、虚拟环境、工具、源码克隆均落于此
    DATA_ROOT: Path = _DATA_ROOT

    # 资源目录：app/resources
    RESOURCES_DIR: Path = APP_ROOT / "app" / "resources"

    # LOGO 目录：app/resources/logo（Windows 用 .ico，macOS 用 .icns）
    LOGO_DIR: Path = RESOURCES_DIR / "logo"

    # 图标目录：app/resources/icons（界面控件用 SVG 图标，如设置齿轮）
    ICONS_DIR: Path = RESOURCES_DIR / "icons"

    # 大模型根目录（可被环境变量 SUBDUB_MODELS_ROOT 覆盖，
    # 供用户把 15GB+ 的模型放到其他分区）
    MODELS_DIR: Path = DATA_ROOT / "models"

    # 工具目录（llama.cpp 预编译 llama-server 等）
    TOOLS_DIR: Path = DATA_ROOT / "tools"

    # 第三方源码目录（IndexTTS 引擎源码克隆）
    THIRD_PARTY_DIR: Path = DATA_ROOT / "third_party"

    # 输出目录（任务产物默认输出到此处，可在任务设置中修改）
    OUTPUT_DIR: Path = DATA_ROOT / "output"

    # 内嵌便携 CPython（冻结态创建 .venv-tts / .venv-ml 的基础解释器；
    # 由安装包携带 python-build-standalone 的 install_dir 发行版）
    RUNTIME_PYTHON_DIR: Path = APP_ROOT / "runtime" / "python"

    @classmethod
    def app_icon_path(cls) -> str | None:
        """按平台返回应用 LOGO 文件路径，不存在返回 None。"""
        filename = "E-SubDub.icns" if sys.platform == "darwin" else "E-SubDub.ico"
        path = cls.LOGO_DIR / filename
        return str(path) if path.is_file() else None

    @classmethod
    def runtime_python_exe(cls) -> Path:
        """内嵌便携 CPython 解释器路径。"""
        return cls.RUNTIME_PYTHON_DIR / (
            "python.exe" if sys.platform == "win32" else "bin" / "python3"
        )


def sanitized_subprocess_env() -> dict[str, str]:
    """构造派生 Python 子进程（.venv-ml / .venv-tts worker 等）用的环境。

    冻结态主程序目录内是 Nuitka 随包分发的大量 DLL（Qt / OpenSSL /
    python3xx 等）：若该目录留在子进程的 PATH 里，子进程 venv 内加载
    的原生扩展（torch 的 c10.dll 等）按 PATH 解析依赖时可能优先命中
    随包 DLL，造成版本不匹配——典型症状即 torch 导入报
    “WinError 1114 动态链接库初始化例程失败”。此处把冻结可执行
    所在目录从 PATH 剔除，让子进程完全依赖自身 venv 的依赖解析；
    开发态原样复制环境。

    返回的 dict 归调用方所有，可继续追加变量（如 venv 的 Scripts 路径）。
    """
    env = dict(os.environ)
    if is_frozen():
        exe_dir = str(Path(sys.executable).resolve().parent)
        parts = [
            p for p in env.get("PATH", "").split(os.pathsep)
            if p and Path(p) != Path(exe_dir)
        ]
        env["PATH"] = os.pathsep.join(parts)
    env.setdefault("PYTHONUTF8", "1")
    return env


config = AppConfig()
