# -*- coding: utf-8 -*-
"""任务窗口（非模态）：配置并启动单个任务，展示该任务的流程与日志。

生命周期：
- 主窗口"＋"按钮创建空窗口 → 拖入媒体、设置参数 → 点"开始"入列执行；
- 双击任务列表行可重新打开/聚焦已绑定任务的窗口（bind_task 恢复状态）；
- 关闭窗口 ≠ 取消任务：任务未终结时仅隐藏窗口，终结后才真正销毁；
- 流程步骤完成后可点击，打开对应流程的结果查看窗口。
"""

from pathlib import Path

from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from app.core.i18n import get_setting, set_setting, tr
from app.core.runner import TaskConfig
from app.core.task_manager import TaskItem, TaskManager, TaskState
from app.core.workspace import STAGE_REVIEW
from app.pipeline.llm_engine import resolve_llama_server_exe
from app.resources.model import get_resource_manager
from app.ui.widgets.import_area import ImportArea
from app.ui.widgets.pipeline_panel import (
    DUBBING_STEP_IDS,
    PIPELINE_STEPS,
    STEP_ACTIVE,
    STEP_DISABLED,
    STEP_DONE,
    STEP_PENDING,
    PipelinePanel,
)
from app.ui.widgets.task_settings import (
    SUBTITLE_BILINGUAL_SOURCE,
    SUBTITLE_BILINGUAL_TARGET,
    TaskSettingsPanel,
)


def task_category_text(dubbing: bool, subtitle_mode: str) -> str:
    """任务类别（固定中文：工作夹名后缀 + 任务列表显示名）。"""
    bilingual = subtitle_mode in (SUBTITLE_BILINGUAL_TARGET, SUBTITLE_BILINGUAL_SOURCE)
    if dubbing:
        return "生成配音和双语字幕" if bilingual else "生成配音和单语字幕"
    return "双语字幕" if bilingual else "单语字幕"


class TaskWindow(QDialog):
    """单个任务的配置与监控窗口。"""

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

    # ------------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        self.setWindowTitle(tr("taskwindow.new_title"))
        # 步骤标签带序号后流程行更宽：放宽任务窗口最小/默认宽度
        self.setMinimumSize(1180, 820)
        self.resize(1180, 860)

        self._import_area = ImportArea()
        self._import_area.files_added.connect(self._on_files_added)
        self._import_area.files_changed.connect(self._on_files_changed)

        self._task_settings = TaskSettingsPanel()

        # 自动通过人工审核（挂机批量）
        auto_row = QHBoxLayout()
        auto_row.setContentsMargins(0, 0, 0, 0)
        self._auto_accept_check = QCheckBox(tr("taskwindow.auto_accept"))
        self._auto_accept_check.setToolTip(tr("taskwindow.auto_accept_tip"))
        # TOOLTIP 不限时显示：用户看完前不自动消失，移开鼠标才隐藏
        self._auto_accept_check.setToolTipDuration(24 * 60 * 60 * 1000)
        auto_row.addWidget(self._auto_accept_check)
        auto_row.addStretch(1)

        self._panel = PipelinePanel()
        self._panel.stage_clicked.connect(self._on_stage_clicked)
        # 配音开关：关闭（仅字幕模式）时流程面板置灰人声分离/配音生成/混音/封装
        self._task_settings.dubbing_enabled_changed.connect(
            self._panel.set_dubbing_enabled
        )
        self._task_settings.dubbing_enabled_changed.connect(
            lambda on: self._panel.log(
                tr("main.log.dubbing_on" if on else "main.log.dubbing_off")
            )
        )
        self._panel.set_dubbing_enabled(self._task_settings.dubbing_enabled())

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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._import_area, 1)
        layout.addWidget(self._task_settings)
        layout.addLayout(auto_row)
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

        cfg = item.config
        if self._import_area.files() != [cfg.media_path]:
            self._import_area.set_file(cfg.media_path)
        self._task_settings.set_selected_codes(
            cfg.source_lang_code, cfg.target_lang_code, cfg.subtitle_mode,
            cfg.dubbing_enabled, cfg.overlay_original_audio, cfg.output_dir,
        )
        self._auto_accept_check.setChecked(cfg.auto_accept_reviews)
        self._restore_pipeline(item)
        self._sync_controls()

    def _restore_pipeline(self, item: TaskItem) -> None:
        """按 manifest 与任务状态恢复流程步骤显示。"""
        dub = item.config.dubbing_enabled
        for step_id, _key in PIPELINE_STEPS:
            if not dub and step_id in DUBBING_STEP_IDS:
                self._panel.set_step_state(step_id, STEP_DISABLED)
            elif item.workspace.is_done(self._done_stage_for(step_id)):
                self._panel.set_step_state(step_id, STEP_DONE)
            elif item.state == TaskState.RUNNING and step_id == item.current_stage:
                self._panel.set_step_state(step_id, STEP_ACTIVE)
            else:
                self._panel.set_step_state(step_id, STEP_PENDING)
        self._panel.set_progress(item.progress)

    @staticmethod
    def _done_stage_for(step_id: str) -> str:
        """步骤显示完成所依据的内部阶段。

        "proofread"（文本审核）= 人工审核：人工审核（内部 STAGE_REVIEW）
        完成才算步骤完成；其余步骤与自身阶段一致。
        """
        return STAGE_REVIEW if step_id == "proofread" else step_id

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
        files = self._import_area.files()
        if not files:
            self._panel.log(tr("main.log.no_file"))
            return
        if not self._validate_and_confirm():
            return
        config = self._build_task_config(files[0])
        if config is None:
            return

        self._panel.log(tr(
            "main.log.task_settings",
            src=self._task_settings.source_language(),
            dst=self._task_settings.target_language(),
        ))
        self._reset_pipeline_steps(config.dubbing_enabled)
        try:
            item = self._manager.enqueue(config)
        except ValueError as exc:
            self._panel.log(str(exc))
            return
        self._task_id = item.id
        self._manager.register_window(item.id, self)
        self.setWindowTitle(item.display_name)
        self._batch_ids = [item.id]

        # 批量（多文件）：其余文件按相同设置入列主窗口任务队列；
        # 导入区与流程进度区仅显示当前执行的文件
        if len(files) > 1:
            self._panel.log(tr("main.log.batch_started", count=len(files)))
            for path in files[1:]:
                batch_config = self._build_task_config(path)
                if batch_config is None:
                    continue
                try:
                    batch_item = self._manager.enqueue(batch_config)
                except ValueError as exc:
                    self._panel.log(str(exc))
                    continue
                self._batch_ids.append(batch_item.id)
            self._panel.log(tr(
                "main.log.batch_queued", count=len(self._batch_ids) - 1,
            ))
            self._import_area.set_file(files[0])
        self._sync_controls()

    def _reset_pipeline_steps(self, dubbing: bool) -> None:
        """启动前重置步骤状态（配音关闭时保持置灰）。"""
        for step_id, _key in PIPELINE_STEPS:
            if not dubbing and step_id in DUBBING_STEP_IDS:
                continue
            self._panel.set_step_state(step_id, STEP_PENDING)
        self._panel.set_progress(0)

    # ------------------------------------------------------------------ 启动校验

    def _validate_and_confirm(self) -> bool:
        """启动前校验资源与授权；全部通过返回 True（失败时已记录日志）。"""
        dubbing = self._task_settings.dubbing_enabled()
        source = self._task_settings.source_language_code()
        target = self._task_settings.target_language_code()
        # 需要调用 LLM 的环节：翻译（源≠目标 或 源语言自动检测）
        need_llm = source != target or source == "auto"

        # 1. 模型资源（ffmpeg 为内置工具，不在此列）
        manager = get_resource_manager()
        required = ["transcribe", "translate"] + (["demucs", "tts"] if dubbing else [])
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

    def _build_task_config(self, media_path: str) -> TaskConfig | None:
        """汇总当前任务设置与资源路径，构造 TaskConfig。"""
        dubbing = self._task_settings.dubbing_enabled()
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
            media_path=media_path,
            output_dir=self._task_settings.output_dir(),
            source_lang_code=source,
            target_lang_code=target,
            subtitle_mode=self._task_settings.subtitle_mode(),
            dubbing_enabled=dubbing,
            device=self._manager.device,
            task_category=task_category_text(dubbing, self._task_settings.subtitle_mode()),
            auto_accept_reviews=self._auto_accept_check.isChecked(),
            overlay_original_audio=self._task_settings.overlay_original_audio(),
            asr_model_dir=resource_manager.get_resource_path("transcribe") or "",
            tts_model_dir=resource_manager.get_resource_path("tts") or "",
            llm_model_path=str(gguf) if gguf else "",
            homura_model_path=homura_gguf,
            llm_server_exe=llm_server,
        )

    # ------------------------------------------------------------------ 导入区事件

    def _on_files_added(self, paths: list) -> None:
        """新文件加入导入区：逐个记录日志。"""
        for path in paths:
            self._panel.log(tr("main.log.imported", name=Path(path).name))

    def _on_files_changed(self) -> None:
        """导入区文件列表变化：未绑定任务时按文件数量刷新开始按钮文案。"""
        if self._current_item() is None:
            self._sync_controls()

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
        kept = [p for p in (result.srt_path, result.mkv_path, result.audio_path) if p]
        self._panel.log(tr("main.log.finished", outputs="、".join(
            Path(p).name for p in kept
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
        inputs = [self._import_area, self._task_settings, self._auto_accept_check]

        if item is None:
            # 多文件 → "开始批量处理"，单文件/未导入 → "开始处理"
            if len(self._import_area.files()) > 1:
                self._start_button.setText(tr("main.start_batch"))
            else:
                self._start_button.setText(tr("main.start"))
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
                or any(item.workspace.stage_dir(s).is_dir() for s, _ in PIPELINE_STEPS)
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
        kept = [
            p for p in (
                str(item.workspace.srt_path(
                    item.config.target_lang_code, item.config.source_lang_code,
                    source_on_top=(
                        item.config.subtitle_mode == SUBTITLE_BILINGUAL_SOURCE
                    ),
                )),
                str(item.workspace.output_mkv),
                str(item.workspace.dubbed_wav),
            ) if Path(p).is_file()
        ]
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

    def _on_stage_clicked(self, step_id: str) -> None:
        """点击已完成的流程步骤：打开结果查看窗口。"""
        item = self._current_item()
        if item is None:
            return
        from app.ui.dialogs.stage_viewers import open_stage_viewer

        open_stage_viewer(item, step_id, parent=self)

    # ------------------------------------------------------------------ 窗口关闭

    def closeEvent(self, event) -> None:
        """任务未终结时仅隐藏窗口（关闭 ≠ 取消任务）。"""
        item = self._current_item()
        if item is not None and not TaskState.is_terminal(item.state):
            event.ignore()
            self.hide()
            return
        if self._task_id is not None:
            self._manager.unregister_window(self._task_id)
        super().closeEvent(event)

    def force_close(self) -> None:
        """应用退出时强制销毁（先由主窗口 shutdown 队列）。"""
        if self._task_id is not None:
            self._manager.unregister_window(self._task_id)
        self.deleteLater()
