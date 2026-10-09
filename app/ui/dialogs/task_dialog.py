# -*- coding: utf-8 -*-
"""任务窗口（非模态）：三类任务各自独立窗口，共用同一骨架。

窗口类层次：
- TaskWindowBase：通用骨架（队列信号接线、开始/停止、批量切换、
  清理中间文件、关闭语义），子类通过钩子定制差异；
- MediaTaskWindowBase：媒体任务（导入区 + 批量入列 + 媒体资源校验）；
- DubbingTaskWindow：配音与字幕（10 步全流程）；
- SubtitleTaskWindow：生成字幕（5 步，无配音链路）；
- TtsTaskWindow：文本转语音（3 步：文本处理/语音合成/格式转换，
  纯 TTS 无 ASR/翻译/混音链路，无人工审核环节）。

生命周期：
- 主窗口三个"＋"按钮分别创建对应类型的空窗口 → 导入/填写内容、
  设置参数 → 点"开始"入列执行；
- 双击任务列表行可重新打开/聚焦已绑定任务的窗口（bind_task 恢复状态）；
- 关闭窗口 ≠ 取消任务：任务未终结时仅隐藏窗口，终结后才真正销毁；
- 流程步骤完成后可点击，打开对应流程的结果查看窗口（TTS 打开产物目录）。
"""

import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QEvent, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import get_setting, set_setting, tr
from app.core.langid_util import (
    is_tts_supported,
    langid_available,
    language_display_name,
)
from app.core.runner import TaskConfig
from app.core.task_manager import TaskItem, TaskManager, TaskState
from app.core.tts_runner import TtsTaskConfig, split_sentences
from app.core.workspace import STAGE_REVIEW
from app.pipeline.llm_engine import resolve_llama_server_exe
from app.resources.model import get_resource_manager
from app.ui.widgets.import_area import ImportArea
from app.ui.widgets.pipeline_panel import (
    PIPELINE_STEPS,
    SUBTITLE_PIPELINE_STEPS,
    TTS_PIPELINE_STEPS,
    STEP_ACTIVE,
    STEP_DONE,
    STEP_PENDING,
    PipelinePanel,
)
from app.ui.widgets.task_settings import (
    SUBTITLE_BILINGUAL_SOURCE,
    SUBTITLE_BILINGUAL_TARGET,
    TaskSettingsPanel,
    TtsSettingsPanel,
)
from app.ui.widgets.audio_player import AudioPlayerBar


def task_category_text(dubbing: bool, subtitle_mode: str) -> str:
    """任务类别（固定中文：工作夹名后缀 + 任务列表显示名）。"""
    bilingual = subtitle_mode in (SUBTITLE_BILINGUAL_TARGET, SUBTITLE_BILINGUAL_SOURCE)
    if dubbing:
        return "生成配音和双语字幕" if bilingual else "生成配音和单语字幕"
    return "双语字幕" if bilingual else "单语字幕"


class _DropCard(QFrame):
    """允许拖入指定类型文件的卡片（文本卡拖 .txt、参考音频卡拖音频）。

    拖入合规本地文件（单文件、扩展名匹配）时发射 file_dropped(路径)；
    悬停时高亮（QSS 的 dndHover 动态属性，与导入区同款交互）。
    """

    file_dropped = Signal(str)

    def __init__(self, suffixes: set[str], parent=None) -> None:
        super().__init__(parent)
        self._suffixes = {ext.lower() for ext in suffixes}
        self.setAcceptDrops(True)
        self.setProperty("dndHover", False)

    def _acceptable_file(self, mime) -> str | None:
        """悬停内容是否为单个合规本地文件；是则返回路径。"""
        if not mime.hasUrls() or len(mime.urls()) != 1:
            return None
        url = mime.urls()[0]
        if not url.isLocalFile():
            return None
        path = url.toLocalFile()
        if Path(path).suffix.lower() in self._suffixes:
            return path
        return None

    def _set_hover(self, hovered: bool) -> None:
        if self.property("dndHover") == hovered:
            return
        self.setProperty("dndHover", hovered)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if self._acceptable_file(event.mimeData()) is not None:
            event.acceptProposedAction()
            self._set_hover(True)

    def dragLeaveEvent(self, _event) -> None:
        self._set_hover(False)

    def dropEvent(self, event) -> None:
        self._set_hover(False)
        path = self._acceptable_file(event.mimeData())
        if path is not None:
            event.acceptProposedAction()
            self.file_dropped.emit(path)


class TaskWindowBase(QDialog):
    """任务窗口通用骨架：队列接线、开始/停止、批量切换、清理、关闭。

    子类通过钩子定制：窗口标题、流程步骤、输入区（_build_body）、
    设置面板、校验链、配置构造、产物路径与步骤点击行为。
    """

    # 子类覆盖：流程步骤列表 [(step_id, i18n key)]
    _steps: list[tuple[str, str]] = PIPELINE_STEPS

    def __init__(self, manager: TaskManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._task_id: str | None = None
        # 批量模式下本窗口创建的全部任务 id（按入列顺序；窗口随完成
        # 情况自动切换到下一个未终结的批量任务）
        self._batch_ids: list[str] = []
        self._build_ui()

        # 队列事件（按 task_id 过滤）
        manager.task_state_changed.connect(self._on_task_state)
        manager.task_progress_changed.connect(self._on_task_progress)
        manager.task_step_changed.connect(self._on_task_step)
        manager.task_log_emitted.connect(self._on_task_log)
        manager.task_finished_ok.connect(self._on_task_finished_ok)

    # ------------------------------------------------------------------ 子类钩子

    def _window_title(self) -> str:
        raise NotImplementedError

    def _build_body(self, layout: QVBoxLayout) -> None:
        """子类专属输入区（导入区 / 文本卡+参考音频卡），加入传入布局。"""

    def _create_settings_panel(self):
        """构造任务设置面板（媒体 TaskSettingsPanel / TTS TtsSettingsPanel）。"""
        raise NotImplementedError

    def _show_auto_accept(self) -> bool:
        """是否显示"自动通过人工审核"开关（TTS 任务无审核环节）。"""
        return True

    def _input_widgets(self) -> list:
        """任务执行中需要禁用的输入控件。"""
        widgets = [self._task_settings]
        if self._auto_accept_check is not None:
            widgets.append(self._auto_accept_check)
        return widgets

    def _apply_config(self, cfg) -> None:
        """bind_task 恢复窗口输入（子类按配置类型回填）。"""

    def _validate_and_confirm(self) -> bool:
        """启动前校验资源与授权；全部通过返回 True（失败时已记录日志）。"""
        raise NotImplementedError

    def _build_task_config(self, media_path: str | None = None):
        """汇总当前设置构造任务配置（媒体任务带文件路径参数）。"""
        raise NotImplementedError

    def _collect_configs(self) -> list:
        """校验 + 构造配置列表（首个为主任务；空列表中止启动）。"""
        if not self._validate_and_confirm():
            return []
        config = self._build_task_config()
        return [config] if config is not None else []

    def _launch_log(self, config) -> None:
        """主任务入列前追加的设置日志（媒体任务记录语言方向）。"""

    def _after_batch_queued(self, first_config) -> None:
        """批量任务全部入列后（媒体任务：导入区只保留第一个文件）。"""

    def _done_stage_for(self, step_id: str) -> str:
        """步骤显示完成所依据的内部阶段（默认与步骤 ID 一致）。"""
        return step_id

    def _finished_paths(self, result) -> list[str]:
        """任务完成的产物路径（日志与清理提示用）。"""
        return [p for p in (result.srt_path, result.mkv_path, result.audio_path) if p]

    def _cleanup_products(self, item: TaskItem) -> list[str]:
        """清理中间文件时保留的根目录成品路径。"""
        raise NotImplementedError

    def _on_stage_clicked(self, step_id: str) -> None:
        """点击流程步骤：打开结果查看窗口（带"继续"按钮）。

        定义在基类：媒体任务与文本转语音任务的流程卡片均可点击。
        """
        item = self._current_item()
        if item is None:
            return
        from app.ui.dialogs.stage_viewers import open_stage_viewer

        open_stage_viewer(item, step_id, parent=self, manager=self._manager)

    def _idle_start_text(self) -> str:
        """无任务时开始按钮文案（媒体任务按文件数区分批量）。"""
        return tr("main.start")

    def _release_resources(self) -> None:
        """窗口隐藏/销毁前释放持有的资源（TTS 窗口释放播放器句柄）。"""

    # ------------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        self.setWindowTitle(self._window_title())
        # 步骤标签带序号后流程行更宽：放宽任务窗口最小/默认宽度
        self.setMinimumSize(1180, 820)
        self.resize(1180, 860)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)

        # 子类专属输入区（导入区 / 文本卡+参考音频卡）
        self._build_body(layout)

        self._task_settings = self._create_settings_panel()
        layout.addWidget(self._task_settings)

        # 自动通过人工审核（挂机批量）；TTS 任务无审核环节不显示
        self._auto_accept_check: QCheckBox | None = None
        if self._show_auto_accept():
            auto_row = QHBoxLayout()
            auto_row.setContentsMargins(0, 0, 0, 0)
            self._auto_accept_check = QCheckBox(tr("taskwindow.auto_accept"))
            self._auto_accept_check.setToolTip(tr("taskwindow.auto_accept_tip"))
            # TOOLTIP 不限时显示：用户看完前不自动消失，移开鼠标才隐藏
            self._auto_accept_check.setToolTipDuration(24 * 60 * 60 * 1000)
            auto_row.addWidget(self._auto_accept_check)
            auto_row.addStretch(1)
            layout.addLayout(auto_row)

        self._panel = PipelinePanel(self._steps)
        self._panel.stage_clicked.connect(self._on_stage_clicked)

        self._start_button = QPushButton(tr("main.start"))
        self._start_button.setObjectName("primaryButton")
        self._start_button.clicked.connect(self._on_start_clicked)
        self._cleanup_button = QPushButton(tr("taskwindow.cleanup"))
        self._cleanup_button.clicked.connect(self._on_cleanup_clicked)
        self._cleanup_button.hide()

        bottom = QHBoxLayout()
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.addStretch(1)
        bottom.addWidget(self._cleanup_button)
        bottom.addWidget(self._start_button)

        layout.addWidget(self._panel, 2)
        layout.addLayout(bottom)

    # ------------------------------------------------------------------ 任务绑定

    def bind_task(self, task_id: str) -> None:
        """绑定已有任务：恢复设置、流程状态并接管窗口标题（重开窗口用）。"""
        item = self._manager.get_task(task_id)
        if item is None:
            return
        self._task_id = task_id
        self._manager.register_window(task_id, self)
        self.setWindowTitle(item.display_name)
        self._apply_config(item.config)
        self._restore_pipeline(item)
        self._sync_controls()

    def _restore_pipeline(self, item: TaskItem) -> None:
        """按 manifest 与任务状态恢复流程步骤显示。"""
        for step_id, _key in self._steps:
            if item.workspace.is_done(self._done_stage_for(step_id)):
                self._panel.set_step_state(step_id, STEP_DONE)
            elif item.state == TaskState.RUNNING and step_id == item.current_stage:
                self._panel.set_step_state(step_id, STEP_ACTIVE)
            else:
                self._panel.set_step_state(step_id, STEP_PENDING)
        self._panel.set_progress(item.progress)

    def _current_item(self) -> TaskItem | None:
        if self._task_id is None:
            return None
        return self._manager.get_task(self._task_id)

    # ------------------------------------------------------------------ 开始/停止

    def _on_start_clicked(self) -> None:
        item = self._current_item()
        if item is not None:
            if item.state == TaskState.QUEUED:
                self._manager.cancel(item.id)      # 取消排队
                return
            if item.state == TaskState.RUNNING:
                self._manager.cancel(item.id)      # 停止处理
                self._panel.log(tr("main.log.cancelling"))
                return
            if item.state == TaskState.CANCELLING:
                return
            # 终态任务：解除绑定，作为新任务重新开始
            self._manager.unregister_window(item.id)
            self._task_id = None
        self._start_new_task()

    def _start_new_task(self) -> None:
        configs = self._collect_configs()
        if not configs:
            return
        self._launch_log(configs[0])
        self._reset_pipeline_steps()
        try:
            item = self._manager.enqueue(configs[0])
        except ValueError as exc:
            self._panel.log(str(exc))
            return
        self._task_id = item.id
        self._manager.register_window(item.id, self)
        self.setWindowTitle(item.display_name)
        self._batch_ids = [item.id]

        # 批量（多文件）：其余文件按相同设置入列主窗口任务队列；
        # 导入区与流程进度区仅显示当前执行的文件
        if len(configs) > 1:
            self._panel.log(tr("main.log.batch_started", count=len(configs)))
            for config in configs[1:]:
                try:
                    batch_item = self._manager.enqueue(config)
                except ValueError as exc:
                    self._panel.log(str(exc))
                    continue
                self._batch_ids.append(batch_item.id)
            self._panel.log(tr(
                "main.log.batch_queued", count=len(self._batch_ids) - 1,
            ))
            self._after_batch_queued(configs[0])
        self._sync_controls()

    def _reset_pipeline_steps(self) -> None:
        """启动前重置步骤状态为待处理。"""
        for step_id, _key in self._steps:
            self._panel.set_step_state(step_id, STEP_PENDING)
        self._panel.set_progress(0)

    # ------------------------------------------------------------------ 队列事件

    def _on_task_state(self, task_id: str) -> None:
        if task_id != self._task_id:
            return
        item = self._current_item()
        if item is None:
            return
        if item.state == TaskState.FAILED:
            self._panel.log(tr("main.log.failed", error=item.last_error))
        elif item.state == TaskState.CANCELLED:
            self._panel.log(tr("main.log.cancelled"))
        # 批量：当前文件失败/取消后切换到下一个未终结的批量任务
        if item.state in (TaskState.FAILED, TaskState.CANCELLED):
            self._advance_batch()
            if task_id != self._task_id:
                return
        self._sync_controls()

    def _on_task_progress(self, task_id: str, value: int) -> None:
        if task_id == self._task_id:
            self._panel.set_progress(value)

    def _on_task_step(self, task_id: str, step_id: str, state: str) -> None:
        if task_id == self._task_id:
            self._panel.set_step_state(step_id, state)

    def _on_task_log(self, task_id: str, message: str) -> None:
        if task_id == self._task_id:
            self._panel.log(message)

    def _on_task_finished_ok(self, task_id: str, result) -> None:
        if task_id != self._task_id:
            return
        self._panel.log(tr("main.log.finished", outputs="、".join(
            Path(p).name for p in self._finished_paths(result)
        )))
        # 批量：当前文件完成后切换到下一个未终结的批量任务
        self._advance_batch()
        if task_id != self._task_id:
            return
        self._sync_controls()

    def _advance_batch(self) -> None:
        """批量任务终结后，把窗口切换到下一个未终结的批量任务。

        切换即重新绑定（导入区/流程区只显示当前执行文件）；
        全部批量任务都已终结时保持现状。
        """
        if not self._batch_ids:
            return
        current_id = self._task_id
        for task_id in self._batch_ids:
            if task_id == current_id:
                continue
            item = self._manager.get_task(task_id)
            if item is not None and not TaskState.is_terminal(item.state):
                if current_id is not None:
                    self._manager.unregister_window(current_id)
                self.bind_task(task_id)
                return

    # ------------------------------------------------------------------ 控件状态

    def _sync_controls(self) -> None:
        """按任务状态同步按钮与输入区可用性。"""
        item = self._current_item()
        inputs = self._input_widgets()

        if item is None:
            self._start_button.setText(self._idle_start_text())
            self._start_button.setEnabled(True)
            self._cleanup_button.hide()
            for w in inputs:
                w.setEnabled(True)
            return

        if item.state == TaskState.QUEUED:
            self._start_button.setText(tr("taskwindow.cancel_queue"))
            self._start_button.setEnabled(True)
            self._cleanup_button.hide()
        elif item.state == TaskState.RUNNING:
            self._start_button.setText(tr("main.stop"))
            self._start_button.setEnabled(True)
            self._cleanup_button.hide()
        elif item.state == TaskState.CANCELLING:
            self._start_button.setText(tr("taskwindow.cancelling"))
            self._start_button.setEnabled(False)
            self._cleanup_button.hide()
        else:  # 终态：可重新开始，可清理中间文件
            self._start_button.setText(tr("taskwindow.reprocess"))
            self._start_button.setEnabled(True)
            self._cleanup_button.setVisible(
                item.workspace.manifest_path.is_file()
                or any(
                    item.workspace.stage_dir(s).is_dir()
                    for s, _ in self._steps
                )
            )

        active = item.state in (
            TaskState.QUEUED, TaskState.RUNNING, TaskState.CANCELLING,
        )
        for w in inputs:
            w.setEnabled(not active)

    # ------------------------------------------------------------------ 清理/查看

    def _on_cleanup_clicked(self) -> None:
        """手动清理中间文件（完成后替代原自动弹窗；保留根目录成品）。"""
        item = self._current_item()
        if item is None or not TaskState.is_terminal(item.state):
            return
        kept = self._cleanup_products(item)
        box = QMessageBox(self)
        box.setWindowTitle(tr("main.cleanup_title"))
        box.setText(tr("main.cleanup_question", files="\n".join(
            f"· {Path(p).name}" for p in kept
        ) or tr("taskwindow.cleanup_no_products")))
        delete_button = box.addButton(
            tr("main.cleanup_delete"), QMessageBox.DestructiveRole
        )
        box.addButton(tr("main.cleanup_keep"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is delete_button:
            self._manager.cleanup(item.id)
            self._panel.log(tr("main.cleanup_done"))
            self._sync_controls()

    # ------------------------------------------------------------------ 窗口关闭

    def closeEvent(self, event) -> None:
        """任务未终结时仅隐藏窗口（关闭 ≠ 取消任务）。"""
        item = self._current_item()
        if item is not None and not TaskState.is_terminal(item.state):
            event.ignore()
            self.hide()
            self._release_resources()
            return
        if self._task_id is not None:
            self._manager.unregister_window(self._task_id)
        self._release_resources()
        super().closeEvent(event)

    def force_close(self) -> None:
        """应用退出时强制销毁（先由主窗口 shutdown 队列）。"""
        if self._task_id is not None:
            self._manager.unregister_window(self._task_id)
        self._release_resources()
        self.deleteLater()


class MediaTaskWindowBase(TaskWindowBase):
    """媒体任务窗口骨架：导入区 + 批量入列 + 媒体任务资源校验/配置。"""

    _dubbing_enabled: bool = True   # 子类覆盖（False = 仅字幕）
    _show_overlay: bool = True      # 是否显示"叠加原背景音"开关
    _steps: list[tuple[str, str]] = PIPELINE_STEPS

    # ------------------------------------------------------------------ 钩子实现

    def _window_title(self) -> str:
        raise NotImplementedError

    def _build_body(self, layout: QVBoxLayout) -> None:
        self._import_area = ImportArea()
        self._import_area.files_added.connect(self._on_files_added)
        self._import_area.files_changed.connect(self._on_files_changed)
        layout.addWidget(self._import_area, 1)

    def _create_settings_panel(self) -> TaskSettingsPanel:
        return TaskSettingsPanel(show_overlay=self._show_overlay)

    def _input_widgets(self) -> list:
        return [self._import_area, *super()._input_widgets()]

    def _apply_config(self, cfg: TaskConfig) -> None:
        if self._import_area.files() != [cfg.media_path]:
            self._import_area.set_file(cfg.media_path)
        self._task_settings.set_selected_codes(
            cfg.source_lang_code, cfg.target_lang_code, cfg.subtitle_mode,
            cfg.overlay_original_audio, cfg.output_dir,
        )
        if self._auto_accept_check is not None:
            self._auto_accept_check.setChecked(cfg.auto_accept_reviews)

    def _collect_configs(self) -> list:
        """校验通过后按导入区文件逐个构造配置（首个失败即中止）。"""
        files = self._import_area.files()
        if not files:
            self._panel.log(tr("main.log.no_file"))
            return []
        if not self._validate_and_confirm():
            return []
        first = self._build_task_config(files[0])
        if first is None:
            return []
        configs = [first]
        # 其余文件逐个构造，失败的记日志后跳过
        for path in files[1:]:
            config = self._build_task_config(path)
            if config is not None:
                configs.append(config)
        return configs

    def _launch_log(self, config) -> None:
        self._panel.log(tr(
            "main.log.task_settings",
            src=self._task_settings.source_language(),
            dst=self._task_settings.target_language(),
        ))

    def _after_batch_queued(self, first_config) -> None:
        self._import_area.set_file(first_config.media_path)

    def _done_stage_for(self, step_id: str) -> str:
        """步骤显示完成所依据的内部阶段。

        "proofread"（文本审核）= 人工审核：人工审核（内部 STAGE_REVIEW）
        完成才算步骤完成；其余步骤与自身阶段一致。
        """
        return STAGE_REVIEW if step_id == "proofread" else step_id

    def _cleanup_products(self, item: TaskItem) -> list[str]:
        cfg = item.config
        srt = str(item.workspace.srt_path(
            cfg.target_lang_code, cfg.source_lang_code,
            source_on_top=(cfg.subtitle_mode == SUBTITLE_BILINGUAL_SOURCE),
        ))
        return [
            p for p in (
                srt,
                str(item.workspace.output_mkv),
                str(item.workspace.dubbed_wav),
            ) if Path(p).is_file()
        ]

    def _idle_start_text(self) -> str:
        if len(self._import_area.files()) > 1:
            return tr("main.start_batch")
        return tr("main.start")

    # ------------------------------------------------------------------ 导入区事件

    def _on_files_added(self, paths: list) -> None:
        """新文件加入导入区：逐个记录日志。"""
        for path in paths:
            self._panel.log(tr("main.log.imported", name=Path(path).name))

    def _on_files_changed(self) -> None:
        """导入区文件列表变化：未绑定任务时按文件数量刷新开始按钮文案。"""
        if self._current_item() is None:
            self._sync_controls()

    # ------------------------------------------------------------------ 启动校验

    def _validate_and_confirm(self) -> bool:
        """启动前校验资源与授权；全部通过返回 True（失败时已记录日志）。"""
        dubbing = self._dubbing_enabled
        source = self._task_settings.source_language_code()
        target = self._task_settings.target_language_code()
        # 需要调用 LLM 的环节：翻译（源≠目标 或 源语言自动检测）
        need_llm = source != target or source == "auto"

        # 1. 模型资源（ffmpeg 为内置工具，不在此列）
        manager = get_resource_manager()
        required = ["transcribe", "translate"] + (
            ["demucs", "tts"] if dubbing else []
        )
        missing = [
            tr(f"resource.{key}.feature")
            for key in required
            if not manager.get(key).status.is_ready
        ]
        if missing:
            self._panel.log(tr("main.log.missing_resources", names="、".join(missing)))
            return False

        # 2. TTS 独立运行环境（IndexTTS-2.5 子进程：.venv-tts + third_party/index-tts）
        if dubbing:
            from app.pipeline.tts import tts_runtime_ready

            ok, detail = tts_runtime_ready()
            if not ok:
                self._panel.log(tr("runner.tts_env_missing", detail=detail))
                return False

        # 3. llama-server（LLM 翻译的推理服务；无需翻译时跳过检查）
        if need_llm and not resolve_llama_server_exe():
            self._panel.log(tr("main.log.llm_server_missing"))
            return False

        # 4. 声音克隆使用授权（仅首次询问，记忆在 settings.json）
        if dubbing and get_setting("voice_clone_ack") is not True:
            box = QMessageBox(self)
            box.setWindowTitle(tr("main.voice_clone_title"))
            box.setText(tr("main.voice_clone_text"))
            agree = box.addButton(tr("main.voice_clone_agree"), QMessageBox.YesRole)
            box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not agree:
                return False
            set_setting("voice_clone_ack", True)

        return True

    def _build_task_config(self, media_path: str | None = None) -> TaskConfig | None:
        """汇总当前任务设置与资源路径，构造 TaskConfig。"""
        dubbing = self._dubbing_enabled
        source = self._task_settings.source_language_code()
        target = self._task_settings.target_language_code()
        # 需要调用 LLM 的环节：翻译（与 _validate_and_confirm 一致）
        need_llm = source != target or source == "auto"
        resource_manager = get_resource_manager()

        # LLM GGUF 权重（translate 资源就绪即保证存在 gguf 文件）；
        # 无需 LLM 时不要求（源=目标语言）
        gguf = None
        llm_server = ""
        if need_llm:
            llm_dir = Path(resource_manager.get_resource_path("translate") or "")
            gguf = next(llm_dir.glob("*.gguf"), None)
            if gguf is None:
                self._panel.log(tr(
                    "main.log.missing_resources", names=tr("resource.translate.feature")
                ))
                return None
            llm_server = resolve_llama_server_exe() or ""

        # Homura 时长修正模型（可选资源：就绪才装配，中文源配音任务生效）
        homura_gguf = ""
        if dubbing:
            homura_dir = resource_manager.get_resource_path("homura")
            if homura_dir:
                found = next(Path(homura_dir).glob("*.gguf"), None)
                homura_gguf = str(found) if found else ""

        return TaskConfig(
            media_path=media_path or "",
            output_dir=self._task_settings.output_dir(),
            source_lang_code=source,
            target_lang_code=target,
            subtitle_mode=self._task_settings.subtitle_mode(),
            dubbing_enabled=dubbing,
            device=self._manager.device,
            task_category=task_category_text(dubbing, self._task_settings.subtitle_mode()),
            auto_accept_reviews=(
                self._auto_accept_check is not None
                and self._auto_accept_check.isChecked()
            ),
            overlay_original_audio=self._task_settings.overlay_original_audio(),
            asr_model_dir=resource_manager.get_resource_path("transcribe") or "",
            tts_model_dir=resource_manager.get_resource_path("tts") or "",
            llm_model_path=str(gguf) if gguf else "",
            homura_model_path=homura_gguf,
            llm_server_exe=llm_server,
        )


class DubbingTaskWindow(MediaTaskWindowBase):
    """配音与字幕任务窗口（10 步全流程，恒启用配音）。"""

    _dubbing_enabled = True
    _show_overlay = True
    _steps = PIPELINE_STEPS

    def _window_title(self) -> str:
        return tr("taskwindow.new_dubbing")


class SubtitleTaskWindow(MediaTaskWindowBase):
    """生成字幕任务窗口（6 步：提取/识别/审核/翻译/译文审核/封装输出，无配音链路）。"""

    _dubbing_enabled = False
    _show_overlay = False
    _steps = SUBTITLE_PIPELINE_STEPS

    def _window_title(self) -> str:
        return tr("taskwindow.new_subtitle")


# 参考音频可接受的扩展名（选择/拖入共用）
_REF_AUDIO_EXTS = {".wav", ".mp3", ".flac", ".m4a", ".ogg", ".aac", ".wma"}


def _read_txt_text(path: str) -> str:
    """读取 TXT 文本：UTF-8 优先，解码失败回退 GBK；失败返回空串。"""
    file = Path(path)
    try:
        return file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            return file.read_text(encoding="gbk", errors="replace")
        except OSError:
            return ""
    except OSError:
        return ""


def _default_tts_name() -> str:
    """手动输入文本时的默认任务名：{日期_时分秒} TTS。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S") + " TTS"


def _txt_dedup_key(path: str) -> str:
    """TXT 防重键：规范化绝对路径（Windows 下不区分大小写）。"""
    return os.path.normcase(str(Path(path).resolve()))


class _TtsFileCard(QFrame):
    """文本转语音左栏：拖入/点击选择多个 TXT 文件，表格展示。

    表格列：序号 / 文件名 / 路径 / 字数 / 编辑 / 删除；编辑列按钮经
    edit_requested 信号交窗口层打开该文件的编辑对话框；载入时即读取
    文本缓存（字数统计与构造任务配置用）。交互与媒体导入区一致：
    点击空白选择、拖入追加、路径去重、行删除前确认。
    """

    files_added = Signal(list)       # 本次新加入的路径列表（已去重）
    files_changed = Signal()        # 文件列表内容变化（新增/删除/清空）
    edit_requested = Signal(str)    # 编辑按钮：请求编辑某文件的句子与情绪
    clicked = Signal()              # 点击空白（宿主先折叠输入区再弹文件框）

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("importDropArea")
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

        # 有序文件列表 + 文本/情绪缓存（路径 → 内容）
        self._files: list[str] = []
        self._texts: dict[str, str] = {}
        self._emotions: dict[str, dict] = {}

        # 引导文案（无文件时居中显示）
        self._hint_title = QLabel(tr("tts.drop_hint_title"))
        self._hint_title.setObjectName("importHintTitle")
        self._hint_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_sub = QLabel(tr("tts.drop_hint_sub"))
        self._hint_sub.setObjectName("importHintSub")
        self._hint_sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._hint_ext = QLabel(tr("tts.drop_hint_ext"))
        self._hint_ext.setObjectName("importHintSub")
        self._hint_ext.setAlignment(Qt.AlignmentFlag.AlignCenter)

        # 有文件时的已导入计数
        self._count_label = QLabel(tr("import.imported_count_hint", count=0))
        self._count_label.setObjectName("importCountLabel")
        self._count_label.hide()

        # 文件表格（列：序号/文件名/路径/字数/编辑/删除）
        self._table = QTableWidget(0, 6)
        self._table.setObjectName("importFileTable")
        self._table.setHorizontalHeaderLabels([
            tr("import.col.index"), tr("import.col.name"),
            tr("import.col.path"), tr("tts.col.chars"),
            tr("tts.col.edit"), tr("import.col.delete"),
        ])
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        # 扩展选择：支持 Shift 连选、Ctrl 多选、鼠标拖拽框选
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        header_view = self._table.horizontalHeader()
        header_view.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header_view.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header_view.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self._table.verticalHeader().setDefaultSectionSize(36)
        # 表格区恢复标准箭头光标（导入区整体为手型，提示可点击）
        self._table.setCursor(Qt.CursorShape.ArrowCursor)
        self._table.hide()

        layout = QVBoxLayout(self)
        # 拖入区内部紧凑排布（虚线边框已提供视觉边界，内容优先）
        layout.setContentsMargins(8, 6, 8, 6)
        layout.setSpacing(8)
        # 无文件时上下弹簧（均分剩余空间）把提示文案垂直居中；有文件时
        # 弹簧隐藏，表格独占剩余高度（隐藏控件不参与布局分配）
        self._top_spacer = QWidget()
        layout.addWidget(self._top_spacer, 1)
        layout.addWidget(self._hint_title, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._hint_sub, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._hint_ext, 0, Qt.AlignmentFlag.AlignHCenter)
        # 顶部行：已导入计数（左）+ 删除所选（右），随表格显隐
        self._delete_selected_button = QPushButton(tr("import.delete_selected"))
        self._delete_selected_button.setObjectName("dangerButton")
        self._delete_selected_button.clicked.connect(self._delete_selected)
        self._delete_selected_button.hide()
        header_row = QHBoxLayout()
        header_row.addWidget(self._count_label)
        header_row.addStretch(1)
        header_row.addWidget(self._delete_selected_button)
        layout.addLayout(header_row)
        layout.addWidget(self._table, 1)
        self._bottom_spacer = QWidget()
        layout.addWidget(self._bottom_spacer, 1)

    # ------------------------------------------------------------------ 数据

    def files(self) -> list[str]:
        """已导入的文件路径列表（按导入顺序）。"""
        return list(self._files)

    def text(self, path: str) -> str:
        """载入时缓存的文件文本。"""
        return self._texts.get(path, "")

    def emotions(self, path: str) -> dict:
        """该文件的情绪映射（{句子文本: 情绪 key}）。"""
        return dict(self._emotions.get(path, {}))

    def set_text(self, path: str, text: str) -> None:
        """编辑对话框确认后回写内存缓存文本（磁盘 TXT 不回写）并刷新表格。"""
        if path in self._texts:
            self._texts[path] = text
            self._refresh()

    def set_emotions(self, path: str, mapping: dict) -> None:
        """编辑确认后回写情绪映射（剔除失配句子）并刷新表格。"""
        text = self._texts.get(path, "")
        valid = set(split_sentences(text))
        self._emotions[path] = {
            k: v for k, v in mapping.items() if k in valid
        }
        self._refresh()

    def clear_files(self) -> None:
        """互斥清空（手动输入接管时调用；空列表不触发信号）。"""
        if not self._files:
            return
        self._files.clear()
        self._texts.clear()
        self._emotions.clear()
        self._refresh()
        self.files_changed.emit()

    # ------------------------------------------------------------------ 文件管理

    def mousePressEvent(self, event) -> None:
        """点击空白：先通知宿主折叠输入区（显示互斥），再弹文件选择框。"""
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            self._choose_files()
        super().mousePressEvent(event)

    def _choose_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self, tr("tts.choose_txt_title"), "",
            f"{tr('tts.txt_filter')} (*.txt);;{tr('import.filter_all')} (*)",
        )
        if paths:
            self.add_files(paths)

    def add_files(self, paths: list) -> None:
        """批量追加 TXT：路径去重 + 读取文本缓存（空/不可读文件跳过）。"""
        existing = {_txt_dedup_key(p) for p in self._files}
        new_files: list[str] = []
        duplicates: list[str] = []
        for path in paths:
            key = _txt_dedup_key(path)
            if key in existing:
                duplicates.append(path)
                continue
            existing.add(key)
            new_files.append(path)
        if duplicates:
            QMessageBox.information(
                self, tr("import.duplicate_title"),
                tr("import.duplicate_found",
                   count=len(duplicates),
                   files="\n".join(f"· {Path(p).name}" for p in duplicates)),
            )
        added: list[str] = []
        empty: list[str] = []
        for path in new_files:
            text = _read_txt_text(path)
            if text.strip():
                self._files.append(path)
                self._texts[path] = text
                added.append(path)
            else:
                empty.append(path)
        if empty:
            QMessageBox.information(
                self, tr("tts.empty_txt_title"),
                tr("tts.empty_txt_text",
                   files="\n".join(f"· {Path(p).name}" for p in empty)),
            )
        if added:
            self._refresh()
            self.files_added.emit(added)
            self.files_changed.emit()

    def _delete_one(self, path: str) -> None:
        """行删除按钮：确认后从列表移除该文件。"""
        name = Path(path).name
        box = QMessageBox(self)
        box.setWindowTitle(tr("import.confirm_delete_title"))
        box.setText(tr("import.confirm_delete_files", count=1, files=f"· {name}"))
        delete_button = box.addButton(tr("common.delete"), QMessageBox.DestructiveRole)
        box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        self._files.remove(path)
        self._texts.pop(path, None)
        self._emotions.pop(path, None)
        self._refresh()
        self.files_changed.emit()

    def _delete_selected(self) -> None:
        """删除所选按钮：确认后批量移除选中行文件（与媒体导入区同款）。"""
        rows = sorted({
            index.row() for index in self._table.selectionModel().selectedRows()
        })
        paths = [self._files[r] for r in rows if r < len(self._files)]
        if not paths:
            return
        box = QMessageBox(self)
        box.setWindowTitle(tr("import.confirm_delete_title"))
        box.setText(tr(
            "import.confirm_delete_files",
            count=len(paths),
            files="\n".join(f"· {Path(p).name}" for p in paths),
        ))
        delete_button = box.addButton(tr("common.delete"), QMessageBox.DestructiveRole)
        box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is not delete_button:
            return
        for path in paths:
            if path in self._files:
                self._files.remove(path)
                self._texts.pop(path, None)
                self._emotions.pop(path, None)
        self._refresh()
        self.files_changed.emit()

    # ------------------------------------------------------------------ 刷新

    def _refresh(self) -> None:
        """文件变化后整体刷新：提示文案、计数与表格。"""
        count = len(self._files)
        self._count_label.setText(tr("import.imported_count_hint", count=count))

        self._table.setRowCount(0)
        for row, path in enumerate(self._files):
            text = self._texts.get(path, "")
            sentences = split_sentences(text)

            index = QTableWidgetItem(str(row + 1))
            index.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            name_item = QTableWidgetItem(Path(path).name)
            name_item.setToolTip(path)
            path_item = QTableWidgetItem(path)
            chars = QTableWidgetItem(tr(
                "tts.char_count", count=len(text), sentences=len(sentences),
            ))
            chars.setTextAlignment(Qt.AlignmentFlag.AlignCenter)

            # 编辑按钮：文案即该文件的编辑摘要（N 句已定制情绪），点击交
            # 窗口层编辑句子与情绪
            emotion = self._emotions.get(path, {})
            edit_button = QPushButton(
                tr("tts.emo_custom_count", count=len(emotion)) if emotion
                else tr("tts.emo_follow_all")
            )
            edit_button.setFixedHeight(26)
            edit_button.setToolTip(tr("tts.edit_tip"))
            edit_button.clicked.connect(
                lambda _checked=False, p=path: self.edit_requested.emit(p)
            )

            delete = QPushButton(tr("common.delete"))
            delete.setObjectName("rowDeleteButton")
            delete.setFixedHeight(26)
            delete.clicked.connect(
                lambda _checked=False, p=path: self._delete_one(p)
            )

            self._table.insertRow(row)
            self._table.setItem(row, 0, index)
            self._table.setItem(row, 1, name_item)
            self._table.setItem(row, 2, path_item)
            self._table.setItem(row, 3, chars)
            self._table.setCellWidget(row, 4, edit_button)
            self._table.setCellWidget(row, 5, delete)

        has_files = count > 0
        self._table.setVisible(has_files)
        self._count_label.setVisible(has_files)
        self._delete_selected_button.setVisible(has_files)
        self._hint_title.setVisible(not has_files)
        self._hint_sub.setVisible(not has_files)
        self._hint_ext.setVisible(not has_files)
        self._top_spacer.setVisible(not has_files)
        self._bottom_spacer.setVisible(not has_files)

    # ------------------------------------------------------------------ 拖拽

    def _is_acceptable(self, path: str) -> bool:
        return Path(path).suffix.lower() == ".txt"

    def _set_hover(self, active: bool) -> None:
        """拖拽悬停时高亮边框（动态属性触发 QSS 状态切换）。"""
        self.setProperty("dndHover", active)
        self.style().unpolish(self)
        self.style().polish(self)

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.isLocalFile() and self._is_acceptable(url.toLocalFile()):
                    self._set_hover(True)
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dragLeaveEvent(self, event) -> None:
        self._set_hover(False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event) -> None:
        self._set_hover(False)
        paths = [
            url.toLocalFile() for url in event.mimeData().urls()
            if url.isLocalFile() and self._is_acceptable(url.toLocalFile())
        ]
        if paths:
            self.add_files(paths)
            event.acceptProposedAction()
            return
        event.ignore()


class _ManualTextCard(QFrame):
    """文本转语音右栏：未展开时显示引导文案，点击任意处展开输入框。"""

    clicked = Signal()

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.setObjectName("ttsManualCard")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # 虚线边框暗示可点击区域（与左栏导入区实线区分）
        self.setStyleSheet(
            "QFrame#ttsManualCard { border: 1px dashed #D1D5DB; border-radius: 8px; }"
        )

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class TtsTaskWindow(TaskWindowBase):
    """文本转语音任务窗口：文本卡（左右两栏）+ 参考音频卡 + 3 步流程。

    文本来源互斥（后到者清空另一侧）：
    - 左栏拖入/点击选择一个或多个 TXT 文件，表格展示（字数/情绪/删除），
      情绪按文件逐个定制，任务名取文件名，支持批量多任务；
    - 右栏点击展开输入框，手动输入或粘贴文本，任务名默认
      {日期_时分秒} TTS（用户改名后不再覆盖）；
    参考音频可拖入或选择，试听条随窗口隐藏/销毁释放文件句柄；
    "自动识别"语言选项按当前文本内容动态更新（如"自动识别（德语）"）。
    """

    _steps = TTS_PIPELINE_STEPS

    def __init__(self, manager: TaskManager, parent=None) -> None:
        # 情绪映射 {句子文本: 情绪 key}（手动输入模式；文件模式在 _TtsFileCard）
        self._emotions: dict = {}
        # 最近一次程序填充的默认任务名（名字仍为它或为空时可刷新）
        self._last_default_name = ""
        self._ref_audio_path: str = ""
        # 最近一次语言检测的结果（启动校验的"不支持语种确认"用）
        self._detected_lang: str = ""
        # 输入法组合态标志（组合期不误收输入区，见 eventFilter）
        self._ime_composing = False
        super().__init__(manager, parent)

    # ------------------------------------------------------------------ 界面

    def _window_title(self) -> str:
        return tr("taskwindow.new_tts")

    def _build_body(self, layout: QVBoxLayout) -> None:
        # ---- 文本卡：左栏文件批量 / 右栏手动输入 + 情绪行 ----
        self._text_card = QFrame()
        self._text_card.setProperty("card", True)
        text_layout = QVBoxLayout(self._text_card)
        text_layout.setContentsMargins(20, 16, 20, 16)
        text_layout.setSpacing(12)

        text_title = QLabel(tr("tts.text_title"))
        text_title.setObjectName("sectionTitle")
        text_layout.addWidget(text_title)

        # 左右两栏各占一半：左 = TXT 文件批量，右 = 手动输入
        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(12)

        self._file_card = _TtsFileCard()
        self._file_card.files_added.connect(self._on_txt_files_added)
        self._file_card.clicked.connect(self._on_file_card_clicked)
        self._file_card.files_changed.connect(self._on_txt_files_changed)
        self._file_card.edit_requested.connect(self._on_file_edit)
        content_row.addWidget(self._file_card, 1)

        self._manual_card = _ManualTextCard()
        self._manual_card.clicked.connect(self._expand_manual_edit)
        manual_layout = QVBoxLayout(self._manual_card)
        manual_layout.setContentsMargins(16, 14, 16, 14)
        manual_layout.setSpacing(8)
        self._manual_hint = QLabel(tr("tts.manual_hint"))
        self._manual_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._manual_hint.setStyleSheet("color: #6B7280;")
        self._text_edit = QPlainTextEdit()
        self._text_edit.setPlaceholderText(tr("tts.text_placeholder"))
        self._text_edit.textChanged.connect(self._on_text_changed)
        self._text_edit.installEventFilter(self)  # 空文本失焦自动收回
        self._text_edit.hide()  # 未展开：先显示引导文案
        self._char_label = QLabel(tr("tts.char_count", count=0, sentences=0))
        self._char_label.setStyleSheet("color: #6B7280;")
        self._char_label.hide()
        manual_layout.addWidget(self._manual_hint, 1)
        manual_layout.addWidget(self._text_edit, 1)
        manual_layout.addWidget(self._char_label)
        content_row.addWidget(self._manual_card, 1)

        text_layout.addLayout(content_row, 1)

        # 编辑行：[编辑按钮] … [多音字标注提示（中文场景显示）]
        edit_row = QHBoxLayout()
        edit_row.setContentsMargins(0, 0, 0, 0)
        self._edit_button = QPushButton(tr("tts.edit"))
        self._edit_button.clicked.connect(self._on_edit_clicked)
        self._polyphone_hint = QLabel(tr("tts.polyphone_hint"))
        self._polyphone_hint.setStyleSheet("color: #6B7280;")
        self._polyphone_hint.hide()
        edit_row.addWidget(self._edit_button)
        edit_row.addStretch(1)
        edit_row.addWidget(self._polyphone_hint)
        text_layout.addLayout(edit_row)
        layout.addWidget(self._text_card, 2)

        # ---- 参考音频卡：选择/拖入 + 试听 ----
        self._ref_card = _DropCard(_REF_AUDIO_EXTS)
        self._ref_card.setProperty("card", True)
        self._ref_card.file_dropped.connect(self._set_ref_audio)
        ref_layout = QVBoxLayout(self._ref_card)
        ref_layout.setContentsMargins(20, 16, 20, 16)
        ref_layout.setSpacing(12)

        ref_title = QLabel(tr("tts.ref_title"))
        ref_title.setObjectName("sectionTitle")
        ref_layout.addWidget(ref_title)

        ref_row = QHBoxLayout()
        ref_row.setContentsMargins(0, 0, 0, 0)
        choose_button = QPushButton(tr("tts.choose_ref"))
        choose_button.clicked.connect(self._choose_ref_audio)
        self._ref_label = QLabel(tr("tts.ref_unset"))
        self._ref_label.setStyleSheet("color: #6B7280;")
        ref_row.addWidget(choose_button)
        ref_row.addSpacing(12)
        ref_row.addWidget(self._ref_label, 1)
        ref_layout.addLayout(ref_row)

        self._audio_player = AudioPlayerBar()
        ref_layout.addWidget(self._audio_player)
        layout.addWidget(self._ref_card)

        # 语言自动识别防抖（文本输入停止 600ms 后检测）
        self._lang_timer = QTimer(self)
        self._lang_timer.setSingleShot(True)
        self._lang_timer.setInterval(600)
        self._lang_timer.timeout.connect(self._detect_language)

    def _create_settings_panel(self) -> TtsSettingsPanel:
        panel = TtsSettingsPanel()
        # 语言选择切换 → 多音字提示随中文/自动检测中文显隐
        panel.lang_changed.connect(self._sync_polyphone_hint)
        return panel

    def _input_widgets(self) -> list:
        return [self._text_card, self._ref_card, self._task_settings]

    # ------------------------------------------------------------------ 状态恢复

    def _apply_config(self, cfg: TtsTaskConfig) -> None:
        self._text_edit.setPlainText(cfg.text)
        self._set_ref_audio(cfg.ref_audio_path)
        self._task_settings.set_selected_values(
            cfg.task_name, cfg.lang_code, cfg.speed, cfg.generate_srt,
            cfg.output_format, cfg.output_dir,
        )
        self._emotions = dict(cfg.emotions or {})
        # 恢复为手动输入形态：任务名字段复位（文件列表不还原，
        # 文本内容直接进输入框）
        self._edit_button.setVisible(True)
        self._task_settings.set_task_name_enabled(True)

    # ------------------------------------------------------------------ 文本源切换

    def _expand_manual_edit(self) -> None:
        """展开右栏输入区（引导文案 → 输入框 + 字数统计）。显示互斥：
        输入区展开即隐藏左栏拖入区。"""
        self._file_card.hide()
        if self._text_edit.isHidden():
            self._manual_hint.hide()
            self._text_edit.show()
            self._char_label.show()
            self._text_edit.setFocus()

    def _collapse_manual_edit(self) -> None:
        """收回右栏输入区（互斥清空后回到引导态），恢复左栏拖入区。"""
        self._manual_hint.show()
        self._text_edit.hide()
        self._char_label.hide()
        self._file_card.show()

    def _clear_manual_text(self) -> None:
        """互斥：文件模式接管时清空手动文本（不触发 textChanged 联动）。"""
        self._text_edit.blockSignals(True)
        self._text_edit.setPlainText("")
        self._text_edit.blockSignals(False)
        self._collapse_manual_edit()

    def _on_file_card_clicked(self) -> None:
        """点击左栏拖入区：折叠手动输入区（显示互斥）。此时手动文本必为
        空（非空时拖入区不可见，点不到），折叠清空无损失。"""
        if not self._text_edit.isHidden():
            self._clear_manual_text()

    def eventFilter(self, obj, event) -> bool:
        """输入框空文本失焦自动收回右栏（防点开后不输入又无出口的死锁）。

        中文输入法组合期（按第一个键、候选窗弹出）可能触发 FocusOut
        且文本仍为空——跟踪 InputMethod 事件的组合态，组合中不收回；
        否则按第一个键输入区就消失，要输入文字后才重新展开。
        """
        if obj is self._text_edit:
            etype = event.type()
            if etype == QEvent.Type.InputMethod:
                # preedit 非空 = 组合进行中；空 = 已提交/取消组合
                self._ime_composing = bool(event.preeditString())
            elif etype == QEvent.Type.FocusOut:
                if (not self._ime_composing
                        and not self._text_edit.toPlainText().strip()):
                    self._collapse_manual_edit()
            elif etype == QEvent.Type.FocusIn:
                self._ime_composing = False
        return super().eventFilter(obj, event)

    def _on_txt_files_added(self, paths: list) -> None:
        """TXT 文件加入：记日志 + 互斥清空手动文本 + 防抖语言检测。"""
        for path in paths:
            self._panel.log(tr(
                "tts.txt_loaded", name=Path(path).name,
                count=len(self._file_card.text(path)),
            ))
        self._clear_manual_text()
        self._lang_timer.start()

    def _on_txt_files_changed(self) -> None:
        """文件列表变化：编辑行切换、任务名联动、开始按钮文案刷新。"""
        has_files = bool(self._file_card.files())
        # 文件模式：表格独占整行（隐藏右栏手动输入卡）；句子与情绪按文件
        # 在表格中逐个编辑，全局编辑行隐藏
        self._manual_card.setVisible(not has_files)
        self._edit_button.setVisible(not has_files)
        self._sync_task_name_field()
        self._sync_polyphone_hint()
        if self._current_item() is None:
            self._sync_controls()

    def _sync_task_name_field(self) -> None:
        """任务名联动：文件模式随文件名（禁用手输），删光文件后清空。"""
        files = self._file_card.files()
        self._task_settings.set_task_name_enabled(not files)
        if files:
            self._task_settings.set_task_name(Path(files[0]).stem)
        else:
            # 文件删光：清掉文件名占位，手动输入时自动填默认名
            self._task_settings.set_task_name("")

    def _maybe_apply_default_name(self) -> None:
        """手动文本非空时填默认任务名（任务名为空或仍是默认名时刷新，
        用户改名后不再覆盖）。"""
        name = self._task_settings.task_name()
        if name == "" or name == self._last_default_name:
            self._last_default_name = _default_tts_name()
            self._task_settings.set_task_name(self._last_default_name)

    # ------------------------------------------------------------------ 文本/语言

    def _on_text_changed(self) -> None:
        """手动文本变化：展开输入区、互斥清空文件列表、默认任务名、
        字数统计、剔除失配情绪、防抖触发语言检测。"""
        text = self._text_edit.toPlainText()
        if text:
            self._expand_manual_edit()
            if self._file_card.files():
                self._file_card.clear_files()
                self._panel.log(tr("tts.manual_takes_over"))
            self._maybe_apply_default_name()
        else:
            # 文本清空：恢复左栏拖入区（为回到文件模式留出入口）；
            # 输入法组合期文本仍为空，不视为清空
            if not self._ime_composing:
                self._file_card.show()
        sentences = split_sentences(text)
        self._char_label.setText(tr(
            "tts.char_count", count=len(text), sentences=len(sentences),
        ))
        # 文本修改后句子可能改写：情绪映射按当前句子集合剔除失配项
        valid = set(sentences)
        self._emotions = {k: v for k, v in self._emotions.items() if k in valid}
        self._lang_timer.start()

    def _current_source_text(self) -> str:
        """当前生效的文本源内容（文件模式取第一个文件，否则手动输入）。"""
        files = self._file_card.files()
        if files:
            return self._file_card.text(files[0])
        return self._text_edit.toPlainText()

    def _detect_language(self) -> None:
        """文本停止变化后检测语言，更新设置区"自动识别"项的动态文案
        与多音字提示显隐。"""
        from app.core.langid_util import detect_text_language

        self._detected_lang = ""
        if not langid_available():
            self._task_settings.set_auto_lang_label("")
            self._sync_polyphone_hint()
            return
        code = detect_text_language(self._current_source_text())
        self._detected_lang = code
        self._task_settings.set_auto_lang_label(
            language_display_name(code) if code else "",
        )
        self._sync_polyphone_hint()

    def _sync_polyphone_hint(self) -> None:
        """多音字标注提示：手选中文/中英混排，或自动检测为中文时显示。"""
        lang = self._task_settings.lang_code()
        if lang == "auto":
            lang = self._detected_lang
        self._polyphone_hint.setVisible(lang in ("zh", "zhen"))

    # ------------------------------------------------------------------ 参考音频

    def _choose_ref_audio(self) -> None:
        exts = " ".join(f"*{ext}" for ext in sorted(_REF_AUDIO_EXTS))
        path, _ = QFileDialog.getOpenFileName(
            self, tr("tts.choose_ref_title"), "",
            f"{tr('tts.ref_filter')} ({exts});;{tr('import.filter_all')} (*)",
        )
        if path:
            self._set_ref_audio(path)

    def _set_ref_audio(self, path: str) -> None:
        """设置参考音频：更新标签并加载到试听条。"""
        self._ref_audio_path = str(path)
        self._ref_label.setText(Path(path).name)
        self._ref_label.setToolTip(str(path))
        self._audio_player.set_source(str(path))

    # ------------------------------------------------------------------ 编辑

    def _on_edit_clicked(self) -> None:
        """打开编辑对话框（手动输入模式）：改句 / 选情绪 / 删句。

        确定后重组文本回写输入框（\n 连接）；情绪映射同步为新句子集合。
        """
        sentences = split_sentences(self._text_edit.toPlainText())
        if not sentences:
            self._panel.log(tr("tts.no_text_for_edit"))
            return
        from app.ui.dialogs.tts_edit_dialog import TtsEditDialog

        dialog = TtsEditDialog(sentences, self._emotions, self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            kept, emotions = dialog.get_result()
            if not kept:
                self._panel.log(tr("tts.no_text_for_edit"))
                return
            # 先回写文本（textChanged 会按新句子剔除旧情绪映射），再整体
            # 覆盖为对话框返回的映射
            self._text_edit.setPlainText("\n".join(kept))
            self._emotions = emotions

    def _on_file_edit(self, path: str) -> None:
        """表格编辑按钮：打开该文件的编辑对话框（改句 / 选情绪 / 删句）。

        确定后更新内存缓存文本（磁盘 TXT 不回写）与该文件的情绪映射。
        """
        sentences = split_sentences(self._file_card.text(path))
        if not sentences:
            self._panel.log(tr("tts.no_text_for_edit"))
            return
        from app.ui.dialogs.tts_edit_dialog import TtsEditDialog

        dialog = TtsEditDialog(
            sentences, self._file_card.emotions(path), self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            kept, emotions = dialog.get_result()
            if not kept:
                self._panel.log(tr("tts.no_text_for_edit"))
                return
            self._file_card.set_text(path, "\n".join(kept))
            self._file_card.set_emotions(path, emotions)

    # ------------------------------------------------------------------ 启动校验

    def _validate_and_confirm(self) -> bool:
        """TTS 启动前校验：输入完整（文件或手动文本）→ tts 资源 →
        TTS 运行环境 → 声音克隆授权 → 不支持语种确认。"""
        # 1. 文本来源：文件与手动文本至少其一（互斥，文件名即任务名）
        files = self._file_card.files()
        if not files and not split_sentences(self._text_edit.toPlainText()):
            self._panel.log(tr("tts.text_required"))
            return False
        if not files and not self._task_settings.task_name():
            self._panel.log(tr("tts.name_required"))
            return False
        if not self._ref_audio_path or not Path(self._ref_audio_path).is_file():
            self._panel.log(tr("tts.ref_required"))
            return False

        # 2. TTS 模型资源
        manager = get_resource_manager()
        if not manager.get("tts").status.is_ready:
            self._panel.log(tr(
                "main.log.missing_resources", names=tr("resource.tts.feature"),
            ))
            return False

        # 3. TTS 独立运行环境（IndexTTS-2.5 子进程：.venv-tts + third_party/index-tts）
        from app.pipeline.tts import tts_runtime_ready

        ok, detail = tts_runtime_ready()
        if not ok:
            self._panel.log(tr("runner.tts_env_missing", detail=detail))
            return False

        # 4. 声音克隆使用授权（仅首次询问，记忆在 settings.json）
        if get_setting("voice_clone_ack") is not True:
            box = QMessageBox(self)
            box.setWindowTitle(tr("main.voice_clone_title"))
            box.setText(tr("main.voice_clone_text"))
            agree = box.addButton(tr("main.voice_clone_agree"), QMessageBox.YesRole)
            box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not agree:
                return False
            set_setting("voice_clone_ack", True)

        # 5. 语种确认：自动识别 / 手选均可命中 IndexTTS 不原生支持的
        #    语种（模型内部回退通用 token，发音效果不保证）
        lang = self._task_settings.lang_code()
        if lang == "auto":
            if self._detected_lang:
                lang = self._detected_lang
            elif not langid_available():
                self._panel.log(tr("tts.langid_missing_hint"))
        if lang and not is_tts_supported(lang):
            box = QMessageBox(self)
            box.setWindowTitle(tr("tts.unsupported_lang_title"))
            box.setText(tr(
                "tts.unsupported_lang_text", lang=language_display_name(lang),
            ))
            proceed = box.addButton(tr("common.confirm"), QMessageBox.AcceptRole)
            box.addButton(tr("common.cancel"), QMessageBox.RejectRole)
            box.exec()
            if box.clickedButton() is not proceed:
                return False
        return True

    def _collect_configs(self) -> list:
        """文件模式：逐文件构造配置（批量多任务，任务名/文本/情绪取自
        各文件）；手动输入模式：单配置。"""
        files = self._file_card.files()
        if not files:
            return super()._collect_configs()
        if not self._validate_and_confirm():
            return []
        configs = []
        for path in files:
            config = self._build_task_config(path)
            if config is not None:
                configs.append(config)
        return configs

    def _build_task_config(self, media_path: str | None = None) -> TtsTaskConfig:
        """汇总当前设置构造 TtsTaskConfig。

        media_path 为文件模式的路径（文本/任务名/情绪取自该文件）；
        None 为手动输入模式（任务名取设置面板，默认名已自动填充）。
        """
        resource_manager = get_resource_manager()
        if media_path:
            text = self._file_card.text(media_path)
            task_name = Path(media_path).stem
            emotions = dict(self._file_card.emotions(media_path))
        else:
            text = self._text_edit.toPlainText()
            task_name = self._task_settings.task_name()
            emotions = dict(self._emotions)
        return TtsTaskConfig(
            task_name=task_name,
            text=text,
            ref_audio_path=self._ref_audio_path,
            output_dir=self._task_settings.output_dir(),
            lang_code=self._task_settings.lang_code(),
            speed=self._task_settings.speed(),
            generate_srt=self._task_settings.generate_srt(),
            output_format=self._task_settings.output_format(),
            device=self._manager.device,
            emotions=emotions,
            auto_accept_reviews=(
                self._auto_accept_check is not None
                and self._auto_accept_check.isChecked()
            ),
            tts_model_dir=resource_manager.get_resource_path("tts") or "",
        )

    def _idle_start_text(self) -> str:
        if len(self._file_card.files()) > 1:
            return tr("main.start_batch")
        return tr("main.start")

    # ------------------------------------------------------------------ 产物/资源

    def _finished_paths(self, result) -> list[str]:
        return [p for p in (result.audio_path, result.srt_path) if p]

    def _cleanup_products(self, item: TaskItem) -> list[str]:
        cfg = item.config
        paths = [str(item.workspace.tts_audio_path(cfg.output_format))]
        if cfg.generate_srt:
            paths.append(str(item.workspace.tts_srt_path(cfg.lang_code)))
        return [p for p in paths if Path(p).is_file()]

    def _release_resources(self) -> None:
        # 释放音频文件句柄（QMediaPlayer 停止后仍持有音源），
        # 防止窗口隐藏/销毁后文件无法被覆盖或删除
        self._audio_player.release()

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 隐藏时已释放播放器：重新显示后按当前参考音频恢复音源
        if (
            self._ref_audio_path
            and os.path.isfile(self._ref_audio_path)
            and self._audio_player.source() != self._ref_audio_path
        ):
            self._audio_player.set_source(self._ref_audio_path)
