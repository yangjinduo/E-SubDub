# -*- coding: utf-8 -*-
"""任务列表持久化：tasks.json 保存任务配置与队列状态快照。

存于用户配置目录（platformdirs；Windows %LOCALAPPDATA%/ESubDub、
macOS ~/Library/Application Support/ESubDub）。全局设置（界面语言、
声音克隆授权等）同存于该文件的顶层键（由 i18n 读写），任务列表只占
"tasks" 一个键。与工作夹内的 manifest.json 职责不同、互不替代：
manifest 是工作区阶段真值（断点复用的依据，保留在工作夹），tasks.json
是主窗口任务列表的队列快照（配置序列化 + 状态/进度/当前流程），队列
每次变化即写盘。

写盘为原子操作（临时文件 + os.replace），且保留 language /
voice_clone_ack 等其它顶层键；解析失败时重命名为 tasks.json.bad 并
忽略，不阻塞启动。manifest 里的阶段完成标记由工作夹自身维护，恢复
后重入列时自动跳过已完成阶段。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, fields
from pathlib import Path

from app.core.i18n import APP_STATE_VERSION, _config_dir, _write_app_state
from app.core.runner import TaskConfig
from app.core.tts_runner import TtsTaskConfig

logger = logging.getLogger(__name__)

# 队列快照结构版本：与全局状态文件共用顶层 version（i18n 定义）；
# 结构不兼容时旧文件整体忽略（列表为空重新开始）
TASKS_SCHEMA_VERSION = APP_STATE_VERSION

# 配置类型标记（TaskConfig / TtsTaskConfig 反序列化分流）
_TYPE_MEDIA = "media"
_TYPE_TTS = "tts"


def tasks_json_path() -> Path:
    """任务列表持久化文件路径（用户配置目录，含全局设置顶层键）。"""
    return _config_dir() / "tasks.json"


def _config_to_record(config: TaskConfig | TtsTaskConfig) -> dict:
    """任务配置 → 可 JSON 序列化记录（显式类型标记）。"""
    return {
        "type": _TYPE_TTS if isinstance(config, TtsTaskConfig) else _TYPE_MEDIA,
        "fields": asdict(config),
    }


def _config_from_record(record) -> TaskConfig | TtsTaskConfig | None:
    """记录 → 任务配置；类型未知或字段缺失时返回 None。

    仅回填 dataclass 已定义的字段（记录中的多余字段忽略），保持对
    配置结构演进的前向兼容。
    """
    if not isinstance(record, dict):
        return None
    cls = TtsTaskConfig if record.get("type") == _TYPE_TTS else TaskConfig
    data = record.get("fields")
    if not isinstance(data, dict):
        return None
    known = {f.name for f in fields(cls)}
    kwargs = {k: v for k, v in data.items() if k in known}
    try:
        return cls(**kwargs)
    except TypeError:
        return None


def save_tasks(records: list[dict]) -> None:
    """把任务记录列表写入 tasks.json 顶层 tasks 键（原子写；保留
    language / voice_clone_ack 等其它顶层键；失败仅记日志不抛出）。"""
    _write_app_state({
        "version": TASKS_SCHEMA_VERSION,
        "tasks": records,
    })


def load_tasks() -> list[dict]:
    """读取任务记录列表；文件缺失/损坏/版本不符时返回空列表。

    损坏文件重命名为 tasks.json.bad 留档（不阻塞启动），版本不符
    直接忽略（旧结构无迁移价值，列表重新开始）。
    """
    path = tasks_json_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tasks = data.get("tasks")
        if (
            data.get("version") == TASKS_SCHEMA_VERSION
            and isinstance(tasks, list)
        ):
            return [t for t in tasks if isinstance(t, dict)]
        logger.info("tasks.json 版本不符，忽略旧任务列表：%s", path)
        return []
    except (OSError, ValueError) as exc:
        bad = path.with_name("tasks.json.bad")
        try:
            os.replace(path, bad)
            logger.warning("tasks.json 解析失败，已重命名为 %s：%s", bad, exc)
        except OSError:
            logger.warning("tasks.json 解析失败且无法重命名：%s", exc)
        return []


def item_to_record(item) -> dict:
    """TaskItem → 持久化记录（配置 + 队列状态/进度/当前流程）。"""
    return {
        "config": _config_to_record(item.config),
        "state": item.state,
        "progress": int(item.progress),
        "current_stage": str(item.current_stage or ""),
        "last_error": str(item.last_error or ""),
    }


def record_to_item_data(record: dict) -> dict | None:
    """持久化记录 → TaskItem 构造参数（除 id 外）；非法返回 None。"""
    config = _config_from_record(record.get("config"))
    if config is None:
        return None
    return {
        "config": config,
        "state": str(record.get("state") or ""),
        "progress": int(record.get("progress") or 0),
        "current_stage": str(record.get("current_stage") or ""),
        "last_error": str(record.get("last_error") or ""),
    }
