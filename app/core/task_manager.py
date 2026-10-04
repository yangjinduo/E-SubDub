# -*- coding: utf-8 -*-
"""顺序任务队列管理器：一次执行一个任务，完成后自动开始下一个。

模型（ASR/demucs/TTS）不支持并行推理且显存有限，因此队列严格串行：
人工审核暂停点弹出模态对话框，整个队列随之暂停（等待用户确认）；
任务可勾选"自动通过人工审核"（TaskConfig.auto_accept_reviews）实现挂机批量。

TaskManager 运行在主线程，持有当前 PipelineRunner（QThread）的引用；
runner 的信号经队列转发（带 task_id），主窗口任务列表与各任务窗口
按 id 过滤刷新。窗口注册表（task_id → TaskWindow）用于审核对话框
定位父窗口与任务窗口去重。
"""

from __future__ import annotations

import itertools
import logging
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from app.core.i18n import tr
from app.core.runner import PipelineRunner, ReviewResult, TaskConfig
from app.core.workspace import STAGE_EXTRACT, Workspace

logger = logging.getLogger(__name__)


class TaskState:
    """任务状态（严格顺序队列，同一时刻至多一个 running）。"""

    QUEUED = "queued"
    RUNNING = "running"
    CANCELLING = "cancelling"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @classmethod
    def is_terminal(cls, state: str) -> bool:
        return state in (cls.DONE, cls.FAILED, cls.CANCELLED)


@dataclass
class TaskItem:
    """队列中的单个任务（配置 + 运行状态）。"""

    id: str
    config: TaskConfig
    workspace: Workspace
    state: str = TaskState.QUEUED
    progress: int = 0
    current_stage: str = ""
    last_error: str = ""

    @property
    def display_name(self) -> str:
        """任务显示名：文件名_任务类别（与工作夹名一致）。"""
        return f"{Path(self.config.media_path).stem}_{self.config.task_category}"


class TaskManager(QObject):
    """任务队列编排器（主线程）。"""

    # 队列事件（均带 task_id，订阅方自行过滤）
    task_added = Signal(str)                  # 任务入列
    task_removed = Signal(str)                # 终态任务被移除出列表
    task_state_changed = Signal(str)          # 状态变化（Item 内字段已更新）
    task_progress_changed = Signal(str, int)  # 总进度 0-100
    task_step_changed = Signal(str, str, str) # 步骤状态 (task_id, step_id, active/done)
    task_log_emitted = Signal(str, str)       # 任务日志
    task_finished_ok = Signal(str, object)    # 全部完成 (task_id, RunResult)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._tasks: list[TaskItem] = []
        self._runner: PipelineRunner | None = None
        self._current: TaskItem | None = None
        # 终态信号已发但线程尚未完全退出的 runner：保留引用，
        # 待内置 finished 信号送达、线程退出后移除（_finalize_retired）
        self._retired: list[PipelineRunner] = []
        self._id_counter = itertools.count(1)
        # task_id → TaskWindow（窗口生命周期注册表）
        self._windows: dict[str, object] = {}
        # 推理设备（"cuda"/"mps"/"cpu"，主窗口检测后更新；
        # 任务窗口构造 TaskConfig 时读取）
        self.device: str = "cpu"

    # ------------------------------------------------------------------ 查询

    def tasks(self) -> list[TaskItem]:
        """全部任务（按入列顺序）。"""
        return list(self._tasks)

    def get_task(self, task_id: str) -> TaskItem | None:
        for item in self._tasks:
            if item.id == task_id:
                return item
        return None

    def find_active_duplicate(self, config: TaskConfig) -> TaskItem | None:
        """查找未终结的完全重复任务（同媒体 + 同类别 + 同输出目录）。"""
        for item in self._tasks:
            if TaskState.is_terminal(item.state):
                continue
            c = item.config
            if (
                c.media_path == config.media_path
                and c.task_category == config.task_category
                and c.output_dir == config.output_dir
            ):
                return item
        return None

    def has_active_tasks(self) -> bool:
        """是否有未终结任务（排队中/运行中/取消中）。"""
        return any(not TaskState.is_terminal(item.state) for item in self._tasks)

    # ------------------------------------------------------------------ 窗口注册表

    def register_window(self, task_id: str, window) -> None:
        self._windows[task_id] = window

    def unregister_window(self, task_id: str) -> None:
        self._windows.pop(task_id, None)

    def window_for(self, task_id: str):
        return self._windows.get(task_id)

    # ------------------------------------------------------------------ 队列操作

    def enqueue(self, config: TaskConfig) -> TaskItem:
        """任务入列并尝试启动；完全重复（未终结）任务抛 ValueError。"""
        dup = self.find_active_duplicate(config)
        if dup is not None:
            raise ValueError(tr("queue.duplicate", name=dup.display_name))
        item = TaskItem(
            id=f"task-{next(self._id_counter)}",
            config=config,
            workspace=Workspace(
                config.output_dir, config.media_path,
                category=config.task_category,
            ),
        )
        self._tasks.append(item)
        self.task_added.emit(item.id)
        self._start_next()
        return item

    def cancel(self, task_id: str) -> None:
        """取消任务：排队中直接移出队列；运行中请求 runner 取消。"""
        item = self.get_task(task_id)
        if item is None:
            return
        if item.state == TaskState.QUEUED:
            item.state = TaskState.CANCELLED
            self.task_state_changed.emit(item.id)
            self._start_next()
        elif item.state == TaskState.RUNNING:
            item.state = TaskState.CANCELLING
            self.task_state_changed.emit(item.id)
            runner = self._runner
            if runner is not None:
                runner.cancel()

    def retry(self, task_id: str) -> None:
        """重试终态任务（manifest 断点复用，已完成阶段跳过）。"""
        item = self.get_task(task_id)
        if item is None or not TaskState.is_terminal(item.state):
            return
        item.state = TaskState.QUEUED
        item.progress = 0
        item.current_stage = ""
        item.last_error = ""
        self.task_state_changed.emit(item.id)
        self._start_next()

    def remove(self, task_id: str) -> None:
        """把终态任务移出列表（产物文件不受影响）。"""
        item = self.get_task(task_id)
        if item is None or not TaskState.is_terminal(item.state):
            return
        self._tasks.remove(item)
        self.task_removed.emit(item.id)

    def delete_task(self, task_id: str) -> bool:
        """删除任务：排队任务直接移出队列，终态任务移出列表
        （产物文件不受影响）；运行中/取消中不可删，返回 False。"""
        item = self.get_task(task_id)
        if item is None or item.state in (
            TaskState.RUNNING, TaskState.CANCELLING,
        ):
            return False
        self._tasks.remove(item)
        self._windows.pop(task_id, None)
        self.task_removed.emit(item.id)
        return True

    def cleanup(self, task_id: str) -> None:
        """清理任务工作夹的中间文件（保留根目录成品）。"""
        item = self.get_task(task_id)
        if item is not None:
            item.workspace.clean_temp()

    def shutdown(self) -> None:
        """应用退出前停止当前 runner（排队任务保留在列表中不再执行）。"""
        runner = self._runner
        self._runner = None
        self._current = None
        if runner is None or not runner.isRunning():
            return
        runner.cancel()
        if runner.wait(10000):
            return
        # 仍卡在不可中断的长耗时操作（如 LLM HTTP 请求）：保留引用直到
        # 线程退出，避免 GC 销毁运行中的 QThread 导致进程崩溃
        self._retired.append(runner)
        runner.finished.connect(lambda r=runner: self._finalize_retired(r))

    # ------------------------------------------------------------------ 执行

    def _start_next(self) -> None:
        """启动队列中第一个排队任务（已有任务在跑则跳过）。"""
        if self._runner is not None:
            return
        for item in self._tasks:
            if item.state == TaskState.QUEUED:
                self._start(item)
                return

    def _start(self, item: TaskItem) -> None:
        # 注入 TaskManager 持有的同一 Workspace 实例：runner 线程写入的
        # manifest 完成标记对 UI 侧立即可见（重开任务窗口可正确恢复流程状态）
        runner = PipelineRunner(item.config, workspace=item.workspace)
        runner.step_state_changed.connect(
            lambda step, state, it=item: self._on_step(it, step, state)
        )
        runner.progress_changed.connect(
            lambda value, it=item: self._on_progress(it, value)
        )
        runner.log_emitted.connect(
            lambda message, it=item: self.task_log_emitted.emit(it.id, message)
        )
        runner.review_requested.connect(
            lambda payload, it=item: self._on_review_requested(it, payload)
        )
        runner.finished_ok.connect(
            lambda result, it=item: self._on_finished_ok(it, result)
        )
        runner.failed.connect(
            lambda message, it=item: self._on_failed(it, message)
        )
        runner.cancelled.connect(lambda it=item: self._on_cancelled(it))
        # 线程真正结束后释放 C++ 对象（Python 引用到本次运行为止）
        runner.finished.connect(runner.deleteLater)

        self._runner = runner
        self._current = item
        item.state = TaskState.RUNNING
        item.progress = 0
        # 启动即处于第一个流程（提取音频）：主窗口状态列无需等首个
        # step_state_changed 信号就能显示实际流程名
        item.current_stage = STAGE_EXTRACT
        self.task_state_changed.emit(item.id)
        runner.start()

    # ------------------------------------------------------------------ runner 事件

    def _on_step(self, item: TaskItem, step_id: str, state: str) -> None:
        if state == "active":
            item.current_stage = step_id
        self.task_step_changed.emit(item.id, step_id, state)

    def _on_progress(self, item: TaskItem, value: int) -> None:
        item.progress = value
        self.task_progress_changed.emit(item.id, value)

    def _on_finished_ok(self, item: TaskItem, result) -> None:
        item.state = TaskState.DONE
        item.progress = 100
        self._release_runner(item)
        self.task_state_changed.emit(item.id)
        self.task_finished_ok.emit(item.id, result)
        self._start_next()

    def _on_failed(self, item: TaskItem, message: str) -> None:
        item.state = TaskState.FAILED
        item.last_error = message
        self._release_runner(item)
        self.task_state_changed.emit(item.id)
        self._start_next()

    def _on_cancelled(self, item: TaskItem) -> None:
        item.state = TaskState.CANCELLED
        self._release_runner(item)
        self.task_state_changed.emit(item.id)
        self._start_next()

    def _release_runner(self, item: TaskItem) -> None:
        """当前 runner 结束后清理引用（C++ 对象由 deleteLater 释放）。"""
        if self._current is item:
            runner = self._runner
            self._runner = None
            self._current = None
            if runner is not None:
                self._retire_runner(runner)

    def _retire_runner(self, runner: PipelineRunner) -> None:
        """确保 OS 线程完全退出后才允许 runner 的 Python 包装被回收。

        实测 PySide6 6.9.3：终态信号（cancelled/failed/finished_ok）是
        run() 的最后一条语句，queued 送达主线程时线程往往尚未退完；无父
        QThread 的最后一个 Python 引用归零会立即销毁底层 C++ 对象，触发
        "QThread: Destroyed while thread '' is still running" 并导致进程
        崩溃。正常路径 wait() 瞬间返回；超时兜底：保留引用，待内置
        finished 信号送达、线程真正退出后再移除。
        """
        if runner.wait(2000):
            return
        self._retired.append(runner)
        runner.finished.connect(lambda r=runner: self._finalize_retired(r))

    def _finalize_retired(self, runner: PipelineRunner) -> None:
        """内置 finished 信号送达后：等线程真正退出，再释放兜底引用。"""
        runner.wait(2000)
        if runner in self._retired:
            self._retired.remove(runner)

    # ------------------------------------------------------------------ 人工审核

    def _on_review_requested(self, item: TaskItem, payload) -> None:
        """审核暂停点：按类型弹模态对话框（队列随之暂停），回传结果。"""
        # 延迟导入避免 core → ui 的模块级依赖
        from PySide6.QtWidgets import QDialog

        from app.ui.dialogs.asr_review_dialog import AsrReviewDialog
        from app.ui.dialogs.translation_review_dialog import TranslationReviewDialog
        from app.ui.dialogs.tts_review_dialog import TtsReviewDialog
        from app.ui.dialogs.voice_manage_dialog import VoiceManageDialog

        runner = self._runner
        if runner is None:
            return

        parent = self.window_for(item.id) or self.parent()
        if payload.kind == "asr":
            dialog: QDialog = AsrReviewDialog(payload.segments, payload.audio_wav, parent=parent)
        elif payload.kind == "translation":
            dialog = TranslationReviewDialog(
                payload.segments, payload.source_lang, payload.target_lang,
                payload.audio_wav, parent=parent,
            )
        elif payload.kind == "voice":
            dialog = VoiceManageDialog(
                payload.segments, payload.ref_map,
                sample_map=payload.sample_map, parent=parent,
            )
        elif payload.kind == "tts":
            # regen_hook：对话框内即时重生成回调（runner 线程阻塞等待，engine 空闲可安全调用）
            dialog = TtsReviewDialog(
                payload.segments, parent=parent, regen_hook=payload.regen_hook,
            )
        else:
            runner.submit_review(None)
            return

        # 标题带任务名：多任务排队时区分审核归属
        dialog.setWindowTitle(f"{item.display_name} · {dialog.windowTitle()}")

        if dialog.exec() == QDialog.DialogCode.Accepted:
            result = ReviewResult(accepted=True)
            if payload.kind == "voice":
                result = ReviewResult(accepted=True, ref_map=dialog.get_ref_map())
            elif payload.kind == "tts":
                result = ReviewResult(
                    accepted=True, regenerate_indices=dialog.get_regenerate_indices(),
                )
            runner.submit_review(result)
        else:
            runner.submit_review(None)
