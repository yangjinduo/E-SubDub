# -*- coding: utf-8 -*-
"""工作目录管理：按"文件名_任务类别"建工作夹，各流程产物分入带序号的
中文子夹（01_～10_，文件夹按流程顺序排序），manifest 记录阶段状态。

目录布局（layout v5）：
    {输出目录}/{文件名}_{任务类别}/
        manifest.json                     # 阶段状态 + layout 版本
        {文件名}.{语种}.srt               # 成品：字幕（单语 ZH / 双语 ZH_EN）
        {文件名}.dubbed.wav               # 成品：配音音频（音频输入）
        01_音频提取/full_audio.wav
        02_人声分离/{vocals.wav, no_vocals.wav}
        03_语音识别/asr.json
        04_文本审核/reviewed.json         # 人工审核定稿
        04_文本审核/句子切片/ 开始时间-结束时间-说话人ID-文本.wav
            # 审核定稿分段的逐句切片，作 TTS 参考
        05_翻译/translated.json           # LLM 翻译版（pre，配音任务含时长修正）
        06_译文审核/translated.json       # 审核后版（post）
        07_配音生成/{refs/, tts/, tts.json}
            # tts.json = TTS 合成结果（pre，复核前）
        08_配音复核/tts_reviewed.json     # 配音复核后的分段数据（post）
        09_混音/ai_new_audio.wav
        10_封装输出/{文件名}.dubbed.mkv    # 成品：配音视频

重复执行同文件任务时，已完成的阶段（产物文件仍存在）直接跳过并复用产物；
人工审核修改内容后由 runner 显式失效下游阶段。旧版 manifest（无 layout
字段的平铺布局，或子夹序号不同的 v2～v4）加载时视为全新任务全量重跑，
不做迁移。
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from app.core.segment import segments_from_json, segments_to_json
from app.core.segment import Segment

# 阶段标识（与 pipeline_panel 步骤 ID 对应；subtitle 仅内部使用，面板不展示）
STAGE_EXTRACT = "extract"
STAGE_SEPARATE = "separate"
STAGE_ASR = "asr"
STAGE_PROOFREAD = "proofread"
STAGE_REVIEW = "review"
STAGE_TRANSLATE = "translate"
STAGE_REVIEW_TRANSLATION = "review_translation"
STAGE_TTS = "tts"
STAGE_TTS_REVIEW = "tts_review"
STAGE_MIX = "mix"
STAGE_MUX = "mux"

# 各流程产物子夹名（固定中文，带两位序号前缀便于按流程顺序排序；
# 不随界面语言变化：保证断点复用路径稳定，且与"查看流程结果"功能的
# 文件夹一一对应）。
# "review"（人工审核）不再单独成步：动作并入 04_文本审核，其产物与
# 句子切片目录同放 "04_文本审核" 子夹（与 STAGE_PROOFREAD 同名）。
# 配音复核产物单独入 08_配音复核 夹（复核前的合成结果留在 07_配音生成）。
STAGE_DIR_NAMES: dict[str, str] = {
    STAGE_EXTRACT: "01_音频提取",
    STAGE_SEPARATE: "02_人声分离",
    STAGE_ASR: "03_语音识别",
    STAGE_PROOFREAD: "04_文本审核",
    STAGE_REVIEW: "04_文本审核",
    STAGE_TRANSLATE: "05_翻译",
    STAGE_REVIEW_TRANSLATION: "06_译文审核",
    STAGE_TTS: "07_配音生成",
    STAGE_TTS_REVIEW: "08_配音复核",
    STAGE_MIX: "09_混音",
    STAGE_MUX: "10_封装输出",
}

# 旧版（layout v2 子夹无序号 / v3 含 05_人工审核 / v4 文本校对同夹复核
# 产物）目录名：清理工作夹时一并删除
_LEGACY_STAGE_DIR_NAMES: tuple[str, ...] = (
    "音频提取", "人声分离", "语音识别", "文本校对", "人工审核",
    "翻译", "译文审核", "配音生成", "混音", "封装输出",
    "05_人工审核", "06_翻译", "07_译文审核", "08_配音生成", "09_混音", "10_封装输出",
    "04_文本校对", "08_混音", "09_封装输出",
)

# 工作夹布局版本：v5 = 配音复核独立成夹（08_配音复核）、混音/封装后移、
# 文本校对更名文本审核（纯人工审核步骤）；旧版 manifest 加载时清空阶段
# 状态（全量重跑）
_LAYOUT_VERSION = 5

# 各阶段对应的产物文件（相对工作夹）；文件丢失时视为未完成
_STAGE_PRODUCTS: dict[str, tuple[str, ...]] = {
    STAGE_EXTRACT: (f"{STAGE_DIR_NAMES[STAGE_EXTRACT]}/full_audio.wav",),
    STAGE_SEPARATE: (
        f"{STAGE_DIR_NAMES[STAGE_SEPARATE]}/vocals.wav",
        f"{STAGE_DIR_NAMES[STAGE_SEPARATE]}/no_vocals.wav",
    ),
    STAGE_ASR: (f"{STAGE_DIR_NAMES[STAGE_ASR]}/asr.json",),
    STAGE_REVIEW: (f"{STAGE_DIR_NAMES[STAGE_REVIEW]}/reviewed.json",),
    STAGE_TRANSLATE: (f"{STAGE_DIR_NAMES[STAGE_TRANSLATE]}/translated.json",),
    STAGE_REVIEW_TRANSLATION: (
        f"{STAGE_DIR_NAMES[STAGE_REVIEW_TRANSLATION]}/translated.json",
    ),
    STAGE_TTS: (f"{STAGE_DIR_NAMES[STAGE_TTS]}/tts.json",),
    STAGE_TTS_REVIEW: (f"{STAGE_DIR_NAMES[STAGE_TTS_REVIEW]}/tts_reviewed.json",),
    STAGE_MIX: (f"{STAGE_DIR_NAMES[STAGE_MIX]}/ai_new_audio.wav",),
    # mux 成品文件名随媒体/语言变化，在 is_done 中按实际路径特判
}


def _sanitize(name: str) -> str:
    """文件名安全化：去掉路径分隔符与控制字符，空白折叠为下划线。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip()
    return cleaned or "task"


class Workspace:
    """单个任务的工作夹。"""

    def __init__(
        self,
        output_dir: str | Path,
        media_path: str | Path,
        category: str = "",
    ) -> None:
        self.media_path = Path(media_path)
        stem = _sanitize(self.media_path.stem)
        if category:
            self.dir = Path(output_dir) / f"{stem}_{_sanitize(category)}"
        else:
            self.dir = Path(output_dir) / stem
        self.dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.dir / "manifest.json"
        self._manifest: dict = self._load_manifest()

    # ------------------------------------------------------------------ manifest

    def _load_manifest(self) -> dict:
        try:
            if self.manifest_path.is_file():
                data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # 旧版布局（无 layout 字段）阶段状态全部作废，全量重跑
                    if data.get("layout") == _LAYOUT_VERSION:
                        return data
                    data = dict(data)
                    data["stages"] = {}
                    return data
        except (OSError, ValueError):
            pass
        return {
            "media": str(self.media_path), "created": time.time(),
            "stages": {}, "layout": _LAYOUT_VERSION,
        }

    def _save_manifest(self) -> None:
        self._manifest["media"] = str(self.media_path)
        self._manifest["layout"] = _LAYOUT_VERSION
        try:
            self.manifest_path.write_text(
                json.dumps(self._manifest, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            pass

    def is_done(self, stage: str) -> bool:
        """阶段已完成且全部产物文件存在。"""
        if not bool(self._manifest.get("stages", {}).get(stage)):
            return False
        for name in _STAGE_PRODUCTS.get(stage, ()):
            if not (self.dir / name).is_file():
                return False
        if stage == STAGE_MUX and not self.output_mkv.is_file():
            return False  # 封装成品在 10_封装输出 夹内，按实际路径校验
        return True

    def mark_done(self, stage: str) -> None:
        self._manifest.setdefault("stages", {})[stage] = True
        self._save_manifest()

    def reset_stage(self, stage: str) -> None:
        """清除某阶段完成标记（产物文件保留，重跑时覆盖）。"""
        self._manifest.setdefault("stages", {}).pop(stage, None)
        self._save_manifest()

    def reset_stages(self, stages: list[str]) -> None:
        for stage in stages:
            self.reset_stage(stage)

    # ------------------------------------------------------------------ 自定义标记

    def stage_flag(self, key: str):
        """读取自定义标记（影响产物形态的设置快照，None = 未记录）。"""
        return self._manifest.get("flags", {}).get(key)

    def set_stage_flag(self, key: str, value) -> None:
        """写入自定义标记（随 manifest 持久化）。"""
        self._manifest.setdefault("flags", {})[key] = value
        self._save_manifest()

    # ------------------------------------------------------------------ 流程子夹

    def stage_dir(self, stage: str) -> Path:
        """流程子夹路径（不确保存在）。"""
        return self.dir / STAGE_DIR_NAMES[stage]

    def ensure_stage_dir(self, stage: str) -> Path:
        """流程子夹路径（不存在则创建）。"""
        path = self.stage_dir(stage)
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ 产物路径

    @property
    def full_audio(self) -> Path:
        return self.stage_dir(STAGE_EXTRACT) / "full_audio.wav"

    @property
    def vocals(self) -> Path:
        return self.stage_dir(STAGE_SEPARATE) / "vocals.wav"

    @property
    def no_vocals(self) -> Path:
        return self.stage_dir(STAGE_SEPARATE) / "no_vocals.wav"

    @property
    def asr_json(self) -> Path:
        return self.stage_dir(STAGE_ASR) / "asr.json"

    @property
    def reviewed_json(self) -> Path:
        """人工审核后的定稿。"""
        return self.stage_dir(STAGE_REVIEW) / "reviewed.json"

    @property
    def translated_pre(self) -> Path:
        """LLM 翻译结果（译文审核前）。"""
        return self.stage_dir(STAGE_TRANSLATE) / "translated.json"

    @property
    def translated_post(self) -> Path:
        """译文审核后的结果。"""
        return self.stage_dir(STAGE_REVIEW_TRANSLATION) / "translated.json"

    @property
    def tts_json(self) -> Path:
        return self.stage_dir(STAGE_TTS) / "tts.json"

    @property
    def tts_reviewed_json(self) -> Path:
        """配音复核后的分段数据（译文/配音路径的最终形态）。"""
        return self.stage_dir(STAGE_TTS_REVIEW) / "tts_reviewed.json"

    @property
    def tts_dir(self) -> Path:
        return self.stage_dir(STAGE_TTS) / "tts"

    @property
    def ref_dir(self) -> Path:
        return self.stage_dir(STAGE_TTS) / "refs"

    @property
    def seg_slice_dir(self) -> Path:
        """逐句人声切片目录（文本审核步骤人工审核的句子切片文件夹，TTS 参考用）。"""
        return self.stage_dir(STAGE_REVIEW) / "句子切片"

    @property
    def mixed_audio(self) -> Path:
        return self.stage_dir(STAGE_MIX) / "ai_new_audio.wav"

    # 成品（字幕/配音音频在工作夹根目录；配音视频成品在 10_封装输出 夹内）
    def srt_path(
        self,
        target_lang: str,
        source_lang: str = "",
        source_on_top: bool = False,
    ) -> Path:
        """成品字幕路径（工作夹根目录）。

        命名 {文件名}.{语种代号}.srt，代号大写，随任务窗口的字幕形式：
        - 单语字幕：仅目标语种代号（ZH）；
        - 双语字幕：两个语种代号，上位的在前（目标在上 ZH_EN / 源在上 EN_ZH）；
        - 源语言为"自动检测"（无确定代号）或与目标相同时，退化为仅目标语种。
        """
        target = target_lang.upper()
        if source_lang and source_lang != "auto" and source_lang != target_lang:
            source = source_lang.upper()
            codes = f"{source}_{target}" if source_on_top else f"{target}_{source}"
        else:
            codes = target
        return self.dir / f"{self.media_path.stem}.{codes}.srt"

    @property
    def output_mkv(self) -> Path:
        """配音成品 MKV（10_封装输出 夹内，与字幕副本同放）。"""
        return self.stage_dir(STAGE_MUX) / f"{self.media_path.stem}.dubbed.mkv"

    @property
    def dubbed_wav(self) -> Path:
        """音频输入（无视频流可封装）时的配音成品。"""
        return self.dir / f"{self.media_path.stem}.dubbed.wav"

    # ------------------------------------------------------------------ 读写分段

    def save_segments(self, path: Path, segments: list[Segment]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(segments_to_json(segments), encoding="utf-8")

    def load_segments(self, path: Path) -> list[Segment]:
        return segments_from_json(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 清理

    def clean_temp(self) -> None:
        """删除中间流程子夹与 manifest。

        保留成品：根目录的字幕 / 配音音频，以及 10_封装输出 夹
        （配音视频 + 字幕副本）；旧版（无序号 / v3～v4 序号）子夹名一并清理。
        """
        import shutil

        for stage, name in STAGE_DIR_NAMES.items():
            if stage == STAGE_MUX:
                continue  # 封装输出夹内是成品
            target = self.dir / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        for name in _LEGACY_STAGE_DIR_NAMES:
            target = self.dir / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
        manifest = self.manifest_path
        if manifest.is_file():
            try:
                manifest.unlink()
            except OSError:
                pass
