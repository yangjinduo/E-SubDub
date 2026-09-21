"""全局配置（占位）：应用根目录、资源目录、输出目录的路径推导。"""

from pathlib import Path


class AppConfig:
    """全局配置类（占位）。"""

    # 应用根目录（项目根，即 app 包的上一级目录）
    APP_ROOT: Path = Path(__file__).resolve().parents[2]

    # 资源目录：app/resources
    RESOURCES_DIR: Path = APP_ROOT / "app" / "resources"

    # 输出目录（占位，后续任务可调整）
    OUTPUT_DIR: Path = APP_ROOT / "output"


config = AppConfig()
