"""资源（模型 / 工具）数据模型与单例管理器。

资源分六项：音频提取（FFmpeg）、人声分离（Demucs htdemucs）、音频转文字
（MOSS-Transcribe-Diarize）、字幕翻译（Index-Translate-2B）、配音时长修正
（Index-Homura-2B，可选）、声音克隆（IndexTTS-2.5）。

真实检测规则：
- FFmpeg：内置工具，get_ffmpeg_path() 非 None 即就绪；
- Demucs：优先检查 torch hub 缓存（~/.cache/torch/hub/checkpoints/ 下的
  htdemucs .th 权重），models/demucs/htdemucs 目录作为用户手动放置的兜底；
- ASR / LLM / TTS：检查模型根目录（get_models_root()，默认 APP_ROOT/models，
  可用环境变量 SUBDUB_MODELS_ROOT 覆盖）下的默认子目录，详见各 _detect_* 函数。

下载元数据（魔搭仓库 id、匹配模式、体积、默认位置）统一见 RESOURCE_META。
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.config import AppConfig
from app.core.ffmpeg import get_ffmpeg_path

logger = logging.getLogger(__name__)

_GB = 1024 ** 3  # 以 GiB 计


class ResourceStatus(Enum):
    """资源就绪状态。"""

    READY = "ready"
    NOT_READY = "not_ready"

    @property
    def is_ready(self) -> bool:
        return self is ResourceStatus.READY


@dataclass
class ResourceInfo:
    """单项资源信息。

    属性：
        key: 资源唯一标识（如 "ffmpeg"）
        feature_name: 功能名（如 "音频提取"）
        model_name: 模型 / 工具名（如 "FFmpeg"）
        status: 就绪状态
        local_path: 本地安装路径（未就绪时为 None）
        download_size_gb: 需下载大小（GB，未就绪时展示用）
        homepage_url: 模型主页链接
    """

    key: str
    feature_name: str
    model_name: str
    status: ResourceStatus = ResourceStatus.NOT_READY
    local_path: str | None = None
    download_size_gb: float = 0.0
    homepage_url: str = ""


@dataclass(frozen=True)
class ResourceMeta:
    """单项资源的下载元数据。

    属性：
        key: 资源唯一标识（同 ResourceInfo.key）
        repo_id: 魔搭社区仓库 id；空串表示不通过魔搭下载（如内置 FFmpeg）
        allow_patterns: snapshot_download 的文件匹配模式（None 表示整仓下载）
        download_size_gb: 预估下载体积（GB，用于进度百分比与 UI 展示）
        homepage_url: 模型主页链接
        default_subdir: 默认存放子目录（相对 get_models_root()）
    """

    key: str
    repo_id: str = ""
    allow_patterns: list[str] | None = None
    download_size_gb: float = 0.0
    homepage_url: str = ""
    default_subdir: str = ""


# ---------------------------------------------------------------- 模型根目录

# 默认模型根目录：APP_ROOT/models；可被环境变量 SUBDUB_MODELS_ROOT 覆盖
MODELS_ROOT: Path = AppConfig.APP_ROOT / "models"


def get_models_root() -> Path:
    """返回当前生效的模型根目录（环境变量 SUBDUB_MODELS_ROOT 优先）。"""
    override = os.environ.get("SUBDUB_MODELS_ROOT")
    return Path(override) if override else MODELS_ROOT


# ---------------------------------------------------------------- LLM 档位

# LLM 档位表：档位名 -> 模型名 / 仓库 / 默认子目录 / 体积（GB）。
# Index 系列 GGUF 仅发布于 HuggingFace（hf-mirror 可直连），魔搭无对应仓库，
# 应用内下载按钮不可用，需按 homepage_url 手动下载。
LLM_VARIANTS: dict[str, dict] = {
    "index-translate-2b": {
        "model_name": "Index-Translate-2B",
        "repo_id": "mradermacher/Index-Translate-2B-GGUF",
        "default_subdir": "Index-Translate-2B",
        "download_size_gb": 1.3,
        "homepage_url": "https://hf-mirror.com/mradermacher/Index-Translate-2B-GGUF",
    },
}

DEFAULT_LLM_VARIANT = "index-translate-2b"


def _llm_meta(variant: str) -> ResourceMeta:
    """按 LLM 档位构造 translate 资源的下载元数据（仅取 Q4_K_M 量化文件）。"""
    spec = LLM_VARIANTS[variant]
    return ResourceMeta(
        key="translate",
        repo_id=spec["repo_id"],
        allow_patterns=["*Q4_K_M*"],
        download_size_gb=spec["download_size_gb"],
        homepage_url=spec["homepage_url"],
        default_subdir=spec["default_subdir"],
    )


# 六项资源的静态定义：key、功能名、模型名（固定顺序；下载元数据见 RESOURCE_META）
_RESOURCE_DEFINITIONS: list[dict] = [
    {"key": "ffmpeg", "feature_name": "音频提取", "model_name": "FFmpeg"},
    {"key": "demucs", "feature_name": "人声分离", "model_name": "Demucs htdemucs"},
    {"key": "transcribe", "feature_name": "音频转文字", "model_name": "MOSS-Transcribe-Diarize"},
    {"key": "translate", "feature_name": "字幕翻译", "model_name": "Index-Translate-2B"},
    {"key": "homura", "feature_name": "配音时长修正", "model_name": "Index-Homura-2B"},
    {"key": "tts", "feature_name": "声音克隆", "model_name": "IndexTTS-2.5"},
]

# 五项资源的下载元数据表（translate 条目随 set_llm_variant() 重建）
RESOURCE_META: dict[str, ResourceMeta] = {
    "ffmpeg": ResourceMeta(
        key="ffmpeg",
        download_size_gb=0.2,
        homepage_url="https://ffmpeg.org",
    ),
    # Demucs 主用 torch hub 自动缓存；魔搭仓库为 htdemucs 权重的官方重打包镜像
    # （model.safetensors 约 84MB，与官方 .th 同源），排除仓库内的示例音频
    "demucs": ResourceMeta(
        key="demucs",
        repo_id="DiffSynth-Studio/Demucs-Repackage",
        allow_patterns=["model.safetensors", "*.json", "*.md"],
        download_size_gb=0.08,
        homepage_url="https://modelscope.cn/models/DiffSynth-Studio/Demucs-Repackage",
        default_subdir="demucs/htdemucs",
    ),
    "transcribe": ResourceMeta(
        key="transcribe",
        repo_id="openmoss/MOSS-Transcribe-Diarize",
        allow_patterns=None,  # 整仓下载（safetensors 约 1.7GB）
        download_size_gb=1.7,
        homepage_url="https://modelscope.cn/models/openmoss/MOSS-Transcribe-Diarize",
        default_subdir="asr/MOSS-Transcribe-Diarize",
    ),
    "translate": _llm_meta(DEFAULT_LLM_VARIANT),
    # Index-Homura-2B：按目标音节数约束译文的翻译模型（中文源 → en/ja/ar/es），
    # 配音任务中用于压缩超长译文；可选资源，缺失时跳过时长修正不阻塞任务
    "homura": ResourceMeta(
        key="homura",
        repo_id="Abu-Dju/Index-Homura-2B-Q8_0-GGUF",
        allow_patterns=["*.gguf"],
        download_size_gb=1.9,
        homepage_url="https://hf-mirror.com/Abu-Dju/Index-Homura-2B-Q8_0-GGUF",
        default_subdir="Index-Homura-2B",
    ),
    # IndexTTS-2.5：零样本声音克隆（GPT 骨干 + flow-matching 解码 + BigVGAN）。
    # 官方依赖 transformers 4.52，与主环境（ASR 所需 5.x）冲突 → 运行于
    # .venv-tts 子进程（见 app/pipeline/tts.py）；源码 third_party/index-tts
    # 免安装直接导入。辅助模型（w2v-bert-2.0 / BigVGAN / CAMPPlus / 语义
    # 编码器）不在本仓库内，首次推理时自动下载到模型目录 hf_cache/
    # （约 3GB，国内网络自动走魔搭镜像）。
    "tts": ResourceMeta(
        key="tts",
        repo_id="IndexTeam/IndexTTS-2.5",
        allow_patterns=None,  # 整仓下载（config.yaml + gpt.pth + s2mel 等，约 5.1GB）
        download_size_gb=5.1,
        homepage_url="https://modelscope.cn/models/IndexTeam/IndexTTS-2.5",
        default_subdir="tts/IndexTTS-2.5",
    ),
}


# ---------------------------------------------------------------- 通用工具

def dir_size_bytes(path: str | Path) -> int:
    """递归统计目录总字节数（含隐藏文件与 *.incomplete 等临时文件）。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                continue
    return total


def _has_incomplete_files(path: Path) -> bool:
    """目录内是否存在 modelscope 半成品文件（*.incomplete）。"""
    return any(p.is_file() and p.name.endswith(".incomplete") for p in path.rglob("*"))


def move_to_trash(path: str | Path) -> Path:
    """把文件/目录移入同级 .trash 子目录（带时间戳命名，避免覆盖误删）。"""
    path = Path(path)
    trash_dir = path.parent / ".trash"
    trash_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dest = trash_dir / f"{path.name}.{stamp}"
    counter = 0
    while dest.exists():
        counter += 1
        dest = trash_dir / f"{path.name}.{stamp}.{counter}"
    shutil.move(str(path), str(dest))
    return dest


# ---------------------------------------------------------------- 就绪检测

# torch hub 权重缓存目录与 htdemucs 官方权重文件名（demucs 包 torch.hub 下载名）
_TORCH_HUB_CHECKPOINTS = Path.home() / ".cache" / "torch" / "hub" / "checkpoints"
_HTDEMUCS_CHECKPOINT = "955717e8-8726e21a.th"
_HTDEMUCS_MIN_BYTES = 80_000_000  # htdemucs 权重约 84MB，过小视为损坏/不完整


def _find_demucs() -> Path | None:
    """定位 htdemucs 权重：torch hub 缓存优先，models/demucs/htdemucs 手动放置兜底。"""
    if _TORCH_HUB_CHECKPOINTS.is_dir():
        known = _TORCH_HUB_CHECKPOINTS / _HTDEMUCS_CHECKPOINT
        if known.is_file() and known.stat().st_size >= _HTDEMUCS_MIN_BYTES:
            return known
        for th in _TORCH_HUB_CHECKPOINTS.glob("*.th"):
            if "htdemucs" in th.name.lower() and th.stat().st_size >= _HTDEMUCS_MIN_BYTES:
                return th
    # 兜底：用户手动放置权重文件的 models/demucs/htdemucs 目录
    fallback = get_models_root() / RESOURCE_META["demucs"].default_subdir
    if fallback.is_dir():
        for f in fallback.rglob("*"):
            if (
                f.is_file()
                and f.suffix.lower() in {".th", ".pt", ".ckpt", ".safetensors"}
                and f.stat().st_size > 1_000_000
            ):
                return fallback
    return None


def _detect_transcribe() -> Path | None:
    """ASR：目录存在 config.json 且总大小 > 1GB，且无下载半成品文件。"""
    d = get_models_root() / RESOURCE_META["transcribe"].default_subdir
    if not d.is_dir() or not (d / "config.json").is_file():
        return None
    if dir_size_bytes(d) <= _GB or _has_incomplete_files(d):
        return None
    return d


def _detect_translate() -> Path | None:
    """LLM：目录下存在大于 1GB 的 *.gguf 量化文件（半成品名为 *.gguf.incomplete，不匹配）。"""
    d = get_models_root() / RESOURCE_META["translate"].default_subdir
    if not d.is_dir():
        return None
    for gguf in d.glob("*.gguf"):
        if gguf.stat().st_size > _GB:
            return d
    return None


def _detect_homura() -> Path | None:
    """Homura：目录下存在大于 1GB 的 *.gguf（可选资源，缺失返回 None）。"""
    d = get_models_root() / RESOURCE_META["homura"].default_subdir
    if not d.is_dir():
        return None
    for gguf in d.glob("*.gguf"):
        if gguf.stat().st_size > _GB:
            return d
    return None


def _detect_tts() -> Path | None:
    """TTS：模型目录存在 config.yaml 且总大小 > 1GB，且无下载半成品文件。

    仅判定模型权重；独立运行环境（.venv-tts / third_party/index-tts）由
    app.pipeline.tts.tts_runtime_ready() 在任务启动时校验。
    """
    d = get_models_root() / RESOURCE_META["tts"].default_subdir
    if not d.is_dir():
        return None
    if not (d / "config.yaml").is_file():
        return None
    if dir_size_bytes(d) <= _GB or _has_incomplete_files(d):
        return None
    return d


class ResourceManager(QObject):
    """资源单例管理器。

    信号：
        resource_status_changed(str): 某项资源状态变化时发射，参数为资源 key，
        供 UI 刷新对应卡片。
    """

    resource_status_changed = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._resources: dict[str, ResourceInfo] = {}
        self._llm_variant: str = DEFAULT_LLM_VARIANT
        self.refresh_all()

    # ------------------------------------------------------------------ 查询

    def resources(self) -> list[ResourceInfo]:
        """按固定顺序返回五项资源信息。"""
        return list(self._resources.values())

    def get(self, key: str) -> ResourceInfo:
        """按键获取资源信息，不存在时抛出 KeyError。"""
        return self._resources[key]

    def get_resource_path(self, key: str) -> str | None:
        """返回已就绪资源的本地路径（文件或目录）；未就绪返回 None。"""
        info = self._resources.get(key)
        if info is not None and info.status.is_ready:
            return info.local_path
        return None

    # ------------------------------------------------------------------ LLM 档位

    def get_llm_variant(self) -> str:
        """返回当前 LLM 档位名（如 "index-translate-2b"）。"""
        return self._llm_variant

    def set_llm_variant(self, variant: str) -> None:
        """切换 LLM 档位（当前仅 index-translate-2b）。

        本期仅影响元数据（仓库 / 体积 / 模型名）与检测路径，
        UI 接入为后续任务。
        """
        if variant not in LLM_VARIANTS:
            raise ValueError(f"未知 LLM 档位: {variant}（可选: {sorted(LLM_VARIANTS)}）")
        if variant == self._llm_variant:
            return
        self._llm_variant = variant
        RESOURCE_META["translate"] = _llm_meta(variant)
        self.refresh("translate")
        # 模型名 / 体积等展示信息随档位变化，即使状态未变也通知 UI
        self.resource_status_changed.emit("translate")

    # ------------------------------------------------------------------ 刷新

    def refresh_all(self) -> None:
        """刷新全部资源状态（状态变化时逐项发射信号）。"""
        for definition in _RESOURCE_DEFINITIONS:
            self.refresh(definition["key"])

    def refresh(self, key: str) -> None:
        """刷新单项资源状态，状态变化时发射 resource_status_changed。"""
        definition = next(d for d in _RESOURCE_DEFINITIONS if d["key"] == key)
        if key == "ffmpeg":
            self._refresh_ffmpeg()
            return

        meta = RESOURCE_META.get(key)
        if key == "demucs":
            path: Path | None = _find_demucs()
        elif key == "transcribe":
            path = _detect_transcribe()
        elif key == "translate":
            path = _detect_translate()
        elif key == "homura":
            path = _detect_homura()
        elif key == "tts":
            path = _detect_tts()
        else:
            path = None

        # LLM 模型名随当前档位展示，其余用静态定义
        model_name = definition["model_name"]
        if key == "translate":
            model_name = LLM_VARIANTS[self._llm_variant]["model_name"]

        new_info = ResourceInfo(
            key=key,
            feature_name=definition["feature_name"],
            model_name=model_name,
            status=ResourceStatus.READY if path is not None else ResourceStatus.NOT_READY,
            local_path=str(path) if path is not None else None,
            download_size_gb=meta.download_size_gb if meta else 0.0,
            homepage_url=meta.homepage_url if meta else "",
        )
        self._apply(key, new_info, self._resources.get(key))

    def _refresh_ffmpeg(self) -> None:
        """FFmpeg 使用 get_ffmpeg_path() 真实检测。"""
        meta = RESOURCE_META["ffmpeg"]
        path = get_ffmpeg_path()
        old = self._resources.get("ffmpeg")
        new_info = ResourceInfo(
            key="ffmpeg",
            feature_name="音频提取",
            model_name="FFmpeg",
            status=ResourceStatus.READY if path else ResourceStatus.NOT_READY,
            local_path=path,
            download_size_gb=meta.download_size_gb,
            homepage_url=meta.homepage_url,
        )
        self._apply("ffmpeg", new_info, old)

    def _apply(self, key: str, new_info: ResourceInfo, old_info: ResourceInfo | None) -> None:
        """写入新状态；与旧状态不一致时发射刷新信号。"""
        status_changed = old_info is None or old_info.status != new_info.status
        self._resources[key] = new_info
        if status_changed:
            self.resource_status_changed.emit(key)

    # ------------------------------------------------------------------ 删除

    def delete_resource(self, key: str) -> bool:
        """删除资源的本地模型目录/文件（移入同级 .trash，避免误删），随后刷新状态。

        FFmpeg 为内置工具，不支持删除（记录 warning 后返回 False）。
        返回 True 表示已执行删除；False 表示无可删除内容或该资源不支持删除。
        """
        if key == "ffmpeg":
            logger.warning("FFmpeg 为内置工具，不支持删除")
            return False
        info = self._resources.get(key)
        if info is not None and info.local_path:
            target = Path(info.local_path)
        else:
            meta = RESOURCE_META.get(key)
            target = get_models_root() / meta.default_subdir if meta and meta.default_subdir else None
        if target is None or not target.exists():
            logger.warning("资源 %s 没有可删除的本地文件（%s）", key, target)
            return False
        moved = move_to_trash(target)
        logger.info("资源 %s 已移入回收目录 %s", key, moved)
        self.refresh(key)
        return True

    # ------------------------------------------------------------ 供后续任务接入

    def update_status(self, key: str, status: ResourceStatus, local_path: str | None = None) -> None:
        """外部更新某项资源状态（下载 / 删除完成后调用），并通知 UI 刷新。"""
        info = self._resources[key]
        info.status = status
        info.local_path = local_path
        self.resource_status_changed.emit(key)


# 模块级单例：通过 get_resource_manager() 获取
_MANAGER: ResourceManager | None = None


def get_resource_manager() -> ResourceManager:
    """获取 ResourceManager 单例（首次调用时创建）。"""
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ResourceManager()
    return _MANAGER
