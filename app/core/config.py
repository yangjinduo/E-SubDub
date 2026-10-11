"""全局配置：应用信息（名称 / 版本）与目录路径推导。"""

import sys
from pathlib import Path


class AppConfig:
    """全局配置类。"""

    # 应用名称：Minim = minimal（极简）；SubDub = Subtitle + Dubbing
    APP_NAME: str = "Minim SubDub"

    # 应用版本（语义化版本）
    APP_VERSION: str = "0.1.0"

    # 应用根目录（项目根，即 app 包的上一级目录）
    APP_ROOT: Path = Path(__file__).resolve().parents[2]

    # 资源目录：app/resources
    RESOURCES_DIR: Path = APP_ROOT / "app" / "resources"

    # LOGO 目录：app/resources/logo（Windows 用 .ico，macOS 用 .icns）
    LOGO_DIR: Path = RESOURCES_DIR / "logo"

    # 图标目录：app/resources/icons（界面控件用 SVG 图标，如设置齿轮）
    ICONS_DIR: Path = RESOURCES_DIR / "icons"

    # 输出目录（任务产物默认输出到此处，可在任务设置中修改）
    OUTPUT_DIR: Path = APP_ROOT / "output"

    @classmethod
    def app_icon_path(cls) -> str | None:
        """按平台返回应用 LOGO 文件路径，不存在返回 None。"""
        filename = "Minim SubDub.icns" if sys.platform == "darwin" else "Minim SubDub.ico"
        path = cls.LOGO_DIR / filename
        return str(path) if path.is_file() else None


config = AppConfig()
