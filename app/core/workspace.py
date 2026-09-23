# -*- coding: utf-8 -*-
"""工作目录管理：按导入文件名在输出目录下建工作夹，manifest 记录阶段状态。

重复执行同文件任务时，已完成的阶段（产物文件仍存在）直接跳过并复用产物；
人工审核修改内容后由 runner 显式失效下游阶段。
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
STAGE_MIX = "mix"
STAGE_MUX = "mux"

# 各阶段对应的产物文件（相对工作夹）；文件丢失时视为未完成
_STAGE_PRODUCTS: dict[str, tuple[str, ...]] = {
    STAGE_EXTRACT: ("full_audio.wav",),
    STAGE_SEPARATE: ("vocals.wav", "no_vocals.wav"),
    STAGE_ASR: ("asr.json",),
    STAGE_PROOFREAD: ("corrected.json",),
    STAGE_TRANSLATE: ("translated.json",),
    STAGE_TTS: ("tts.json",),
    STAGE_MIX: ("ai_new_audio.wav",),
}

# 审核阶段的产物即对应的 json（人工确认 = 阶段完成）
_STAGE_PRODUCTS[STAGE_REVIEW] = ("corrected.json",)
_STAGE_PRODUCTS[STAGE_REVIEW_TRANSLATION] = ("translated.json",)


def _sanitize(name: str) -> str:
    """文件名安全化：去掉路径分隔符与控制字符，空白折叠为下划线。"""
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", name).strip()
    return cleaned or "task"


class Workspace:
    """单个任务的工作夹。"""

    def __init__(self, output_dir: str | Path, media_path: str | Path) -> None:
        self.media_path = Path(media_path)
        stem = _sanitize(self.media_path.stem)
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
                    return data
        except (OSError, ValueError):
            pass
        return {"media": str(self.media_path), "created": time.time(), "stages": {}}

    def _save_manifest(self) -> None:
        self._manifest["media"] = str(self.media_path)
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

    # ------------------------------------------------------------------ 产物路径

    @property
    def full_audio(self) -> Path:
        return self.dir / "full_audio.wav"

    @property
    def vocals(self) -> Path:
        return self.dir / "vocals.wav"

    @property
    def no_vocals(self) -> Path:
        return self.dir / "no_vocals.wav"

    @property
    def asr_json(self) -> Path:
        return self.dir / "asr.json"

    @property
    def corrected_json(self) -> Path:
        return self.dir / "corrected.json"

    @property
    def translated_json(self) -> Path:
        return self.dir / "translated.json"

    @property
    def tts_json(self) -> Path:
        return self.dir / "tts.json"

    @property
    def tts_dir(self) -> Path:
        return self.dir / "tts"

    @property
    def ref_dir(self) -> Path:
        return self.dir / "refs"

    @property
    def seg_ref_dir(self) -> Path:
        """逐句参考克隆切片目录（随 refs 一并清理）。"""
        return self.ref_dir / "segments"

    @property
    def mixed_audio(self) -> Path:
        return self.dir / "ai_new_audio.wav"

    def srt_path(self, lang_code: str) -> Path:
        return self.dir / f"{self.media_path.stem}.{lang_code}.srt"

    @property
    def output_mkv(self) -> Path:
        return self.dir / f"{self.media_path.stem}.dubbed.mkv"

    # ------------------------------------------------------------------ 读写分段

    def save_segments(self, path: Path, segments: list[Segment]) -> None:
        path.write_text(segments_to_json(segments), encoding="utf-8")

    def load_segments(self, path: Path) -> list[Segment]:
        return segments_from_json(path.read_text(encoding="utf-8"))

    # ------------------------------------------------------------------ 清理

    # 成品之外的中间产物（清理时删除；manifest 保留与否无所谓，一并清理）
    _TEMP_NAMES = (
        "full_audio.wav", "vocals.wav", "no_vocals.wav",
        "asr.json", "corrected.json", "translated.json", "tts.json",
        "tts", "refs", "ai_new_audio.wav", "manifest.json",
    )

    def clean_temp(self) -> None:
        """删除临时文件（保留 SRT 与 MKV 成品）。"""
        import shutil

        for name in self._TEMP_NAMES:
            target = self.dir / name
            if target.is_dir():
                shutil.rmtree(target, ignore_errors=True)
            elif target.is_file():
                try:
                    target.unlink()
                except OSError:
                    pass
