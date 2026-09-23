"""主窗口：组装资源状态栏、导入区、任务设置区、流程进度区。

界面文案统一走 i18n（tr）；语言切换时通过 _build_ui() 整体重建界面，
并保留用户已导入的文件、任务设置与日志内容。

开始按钮接入 PipelineRunner（QThread 编排引擎）：运行中变为"停止处理"，
runner 的信号统一转发到 MainWindow 处理（内部再转到当前面板实例，
语言切换重建面板后依然有效），人工审核暂停点在此分发到四个审核对话框。
"""

from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.hardware import DeviceInfo, detect_device, detect_device_standalone
from app.core.i18n import get_setting, set_setting, tr
from app.core.runner import PipelineRunner, ReviewResult, TaskConfig
from app.pipeline.llm_engine import resolve_llama_server_exe
from app.resources.model import get_models_root, get_resource_manager
from app.ui.dialogs.asr_review_dialog import AsrReviewDialog
from app.ui.dialogs.resource_dialog import SettingsDialog
from app.ui.dialogs.translation_review_dialog import TranslationReviewDialog
from app.ui.dialogs.tts_review_dialog import TtsReviewDialog
from app.ui.dialogs.voice_manage_dialog import VoiceManageDialog
from app.ui.widgets.import_area import ImportArea
from app.ui.widgets.pipeline_panel import (
    DUBBING_STEP_IDS,
    PIPELINE_STEPS,
    STEP_PENDING,
    PipelinePanel,
)
from app.ui.widgets.resource_bar import ResourceStatusBar
from app.ui.widgets.task_settings import TaskSettingsPanel


class MainWindow(QMainWindow):
    """应用主窗口。"""

    def __init__(
        self,
        device_info: DeviceInfo | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        # 资源管理器单例
        self._manager = get_resource_manager()
        # 最新设备检测结果（语言切换重建界面时复用，不重复检测）
        self._device: DeviceInfo = device_info or detect_device_standalone()
        self._device_refreshed = False
        # 当前流水线线程（运行中非 None；引用同时防止 Python 侧被 GC）
        self._runner: PipelineRunner | None = None
        self._build_ui()

    # ------------------------------------------------------------------ 界面构建

    def _build_ui(self) -> None:
        """构建全部界面；语言切换时可重复调用（旧的 central widget 被自动销毁）。"""
        # 重建前保留用户状态（首次构建时无旧界面）
        previous = self._capture_state()

        self.setWindowTitle(tr("main.title"))
        self.setMinimumSize(1100, 750)  # 最小尺寸，窗口可自由缩放

        # ------------------------------ 顶部：资源状态栏 ------------------------------
        self._resource_bar = ResourceStatusBar(self._manager)
        self._resource_bar.set_device(self._device)
        self._resource_bar.settings_requested.connect(self._open_settings_dialog)

        # ------------------------------ 中部：导入区 ------------------------------
        self._import_area = ImportArea()
        self._import_area.file_imported.connect(self._on_file_imported)

        # ------------------------------ 任务设置区 ------------------------------
        self._task_settings = TaskSettingsPanel()
        if previous is not None:
            self._task_settings.load_state(previous["task_state"])

        # ------------------------------ 底部：流程进度区 ------------------------------
        self._pipeline_panel = PipelinePanel()
        if previous is not None and previous["log_text"]:
            self._pipeline_panel.restore_log(previous["log_text"])
        else:
            ready_count = sum(1 for r in self._manager.resources() if r.status.is_ready)
            total_count = len(self._manager.resources())
            self._pipeline_panel.log(
                tr("main.log.init", n=ready_count, total=total_count)
            )
            self._pipeline_panel.log(
                tr("main.log.device_initial", desc=self._device.description)
            )

        # 恢复配音开关对流程面板的影响（load_state 期间信号被阻塞）
        self._pipeline_panel.set_dubbing_enabled(self._task_settings.dubbing_enabled())
        self._task_settings.dubbing_enabled_changed.connect(self._on_dubbing_toggled)

        # 底部主操作按钮
        self._start_button = QPushButton(tr("main.start"))
        self._start_button.setObjectName("primaryButton")
        self._start_button.clicked.connect(self._on_start_clicked)
        bottom_bar = QHBoxLayout()
        bottom_bar.setContentsMargins(0, 0, 0, 0)
        bottom_bar.addStretch(1)
        bottom_bar.addWidget(self._start_button)

        # ------------------------------ 总装 ------------------------------
        central = QWidget()
        layout = QVBoxLayout(central)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(12)
        layout.addWidget(self._resource_bar)
        layout.addWidget(self._import_area, 1)
        layout.addWidget(self._task_settings)
        layout.addWidget(self._pipeline_panel, 2)
        layout.addLayout(bottom_bar)
        self.setCentralWidget(central)

        # 恢复此前导入的文件（会重新探测时长并记录日志）
        if previous is not None and previous["imported_file"]:
            self._import_area.set_file(previous["imported_file"])

        # 界面显示后用 torch 做一次精确设备检测（导入 torch 需数秒，仅首次执行）
        if not self._device_refreshed:
            QTimer.singleShot(300, self._refresh_device)

    def _capture_state(self) -> dict | None:
        """重建前收集旧界面状态；首次构建（无旧界面）返回 None。"""
        if not hasattr(self, "_task_settings"):
            return None
        return {
            "task_state": self._task_settings.state(),
            "log_text": self._pipeline_panel.log_text(),
            "imported_file": self._import_area.current_file,
        }

    # ------------------------------------------------------------------ 设备

    def _refresh_device(self) -> None:
        """基于 torch 的精确设备检测，刷新角标显示。"""
        self._device = detect_device()
        self._resource_bar.set_device(self._device)
        self._pipeline_panel.log(tr("main.log.device_done", desc=self._device.description))
        self._device_refreshed = True

    # ------------------------------------------------------------------ 事件处理

    def _on_file_imported(self, path: str) -> None:
        """导入文件后记录日志。"""
        from pathlib import Path

        self._pipeline_panel.log(tr("main.log.imported", name=Path(path).name))

    def _on_dubbing_toggled(self, enabled: bool) -> None:
        """配音开关切换：同步置灰/恢复流程面板的配音相关步骤。"""
        self._pipeline_panel.set_dubbing_enabled(enabled)
        self._pipeline_panel.log(
            tr("main.log.dubbing_on") if enabled else tr("main.log.dubbing_off")
        )

    def _on_start_clicked(self) -> None:
        """开始处理按钮：运行中点击 = 请求取消；空闲时校验并启动流水线。"""
        runner = self._runner
        if runner is not None and runner.isRunning():
            runner.cancel()
            self._pipeline_panel.log(tr("main.log.cancelling"))
            return

        media_path = self._import_area.current_file
        if media_path is None:
            self._pipeline_panel.log(tr("main.log.no_file"))
            return

        if not self._validate_and_confirm():
            return

        config = self._build_task_config(media_path)
        if config is None:
            return

        self._pipeline_panel.log(tr(
            "main.log.task_settings",
            src=self._task_settings.source_language(),
            dst=self._task_settings.target_language(),
        ))
        self._reset_pipeline_steps(config.dubbing_enabled)
        self._start_runner(config)

    # ------------------------------------------------------------------ 启动校验

    def _validate_and_confirm(self) -> bool:
        """启动前校验资源与授权；全部通过返回 True（失败时已记录日志）。"""
        panel = self._pipeline_panel
        dubbing = self._task_settings.dubbing_enabled()

        # 1. 模型资源（ffmpeg 为内置工具，不在此列）
        required = ["transcribe", "translate"] + (["demucs", "tts"] if dubbing else [])
        missing = [
            tr(f"resource.{key}.feature")
            for key in required
            if not self._manager.get(key).status.is_ready
        ]
        if missing:
            panel.log(tr("main.log.missing_resources", names="、".join(missing)))
            return False

        # 2. TTS 音频编解码器（独立仓库，不在 tts 资源体积内）
        codec_dir = get_models_root() / "tts" / "MOSS-Audio-Tokenizer"
        if dubbing and not (codec_dir / "config.json").is_file():
            panel.log(tr("runner.codec_missing", path=str(codec_dir)))
            return False

        # 3. llama-server（LLM 校对/翻译的推理服务）
        if not resolve_llama_server_exe():
            panel.log(tr("main.log.llm_server_missing"))
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

        # LLM GGUF 权重（translate 资源就绪即保证存在 gguf 文件）
        llm_dir = Path(self._manager.get_resource_path("translate") or "")
        gguf = next(llm_dir.glob("*.gguf"), None)
        if gguf is None:
            self._pipeline_panel.log(tr(
                "main.log.missing_resources", names=tr("resource.translate.feature")
            ))
            return None

        return TaskConfig(
            media_path=media_path,
            output_dir=self._task_settings.output_dir(),
            source_lang_code=self._task_settings.source_language_code(),
            target_lang_code=self._task_settings.target_language_code(),
            subtitle_mode=self._task_settings.subtitle_mode(),
            dubbing_enabled=dubbing,
            device=self._device.device,
            asr_model_dir=self._manager.get_resource_path("transcribe") or "",
            tts_model_dir=self._manager.get_resource_path("tts") or "",
            tts_codec_dir=str(get_models_root() / "tts" / "MOSS-Audio-Tokenizer"),
            llm_model_path=str(gguf),
            llm_server_exe=resolve_llama_server_exe(),
        )

    def _reset_pipeline_steps(self, dubbing: bool) -> None:
        """启动前重置步骤状态（配音关闭时保持置灰）。"""
        for step_id, _key in PIPELINE_STEPS:
            if not dubbing and step_id in DUBBING_STEP_IDS:
                continue
            self._pipeline_panel.set_step_state(step_id, STEP_PENDING)
        self._pipeline_panel.set_progress(0)

    # ------------------------------------------------------------------ 运行控制

    def _start_runner(self, config: TaskConfig) -> None:
        """启动流水线线程并连接信号（均连到 MainWindow，重建面板无需重连）。"""
        runner = PipelineRunner(config)
        runner.step_state_changed.connect(self._on_step_state_changed)
        runner.progress_changed.connect(self._on_progress_changed)
        runner.log_emitted.connect(self._on_runner_log)
        runner.review_requested.connect(self._on_review_requested)
        runner.finished_ok.connect(self._on_finished_ok)
        runner.failed.connect(self._on_failed)
        runner.cancelled.connect(self._on_cancelled)
        # 线程真正结束后释放 C++ 对象（引用由 self._runner 继续持有到本次运行结束）
        runner.finished.connect(runner.deleteLater)
        self._runner = runner
        self._start_button.setText(tr("main.stop"))
        runner.start()

    def _reset_after_run(self) -> None:
        """一次运行结束（完成/失败/取消）后恢复按钮。"""
        self._runner = None
        self._start_button.setText(tr("main.start"))

    def _on_step_state_changed(self, step_id: str, state: str) -> None:
        self._pipeline_panel.set_step_state(step_id, state)

    def _on_progress_changed(self, value: int) -> None:
        self._pipeline_panel.set_progress(value)

    def _on_runner_log(self, message: str) -> None:
        self._pipeline_panel.log(message)

    def _on_failed(self, message: str) -> None:
        self._pipeline_panel.log(tr("main.log.failed", error=message))
        self._reset_after_run()

    def _on_cancelled(self) -> None:
        self._pipeline_panel.log(tr("main.log.cancelled"))
        self._reset_after_run()

    def _on_finished_ok(self, result) -> None:
        """全部完成：记录产物并询问是否清理临时文件。"""
        panel = self._pipeline_panel
        kept = [p for p in (result.srt_path, result.mkv_path, result.audio_path) if p]
        panel.log(tr("main.log.finished", outputs="、".join(
            Path(p).name for p in kept
        )))
        self._reset_after_run()

        box = QMessageBox(self)
        box.setWindowTitle(tr("main.cleanup_title"))
        box.setText(tr("main.cleanup_question", files="\n".join(
            f"· {Path(p).name}" for p in kept
        )))
        delete_button = box.addButton(
            tr("main.cleanup_delete"), QMessageBox.DestructiveRole
        )
        box.addButton(tr("main.cleanup_keep"), QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is delete_button:
            result.workspace.clean_temp()
            panel.log(tr("main.cleanup_done"))

    # ------------------------------------------------------------------ 人工审核

    def _on_review_requested(self, payload) -> None:
        """审核暂停点：按类型弹对话框，确认后回传结果，取消则中止流水线。"""
        runner = self._runner
        if runner is None:
            return

        if payload.kind == "asr":
            dialog = AsrReviewDialog(payload.segments, payload.audio_wav, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                runner.submit_review(ReviewResult(accepted=True))
            else:
                runner.submit_review(None)
        elif payload.kind == "translation":
            dialog = TranslationReviewDialog(
                payload.segments, payload.source_lang, payload.target_lang,
                payload.audio_wav, parent=self,
            )
            if dialog.exec() == QDialog.DialogCode.Accepted:
                runner.submit_review(ReviewResult(accepted=True))
            else:
                runner.submit_review(None)
        elif payload.kind == "voice":
            dialog = VoiceManageDialog(payload.segments, payload.ref_map, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                runner.submit_review(ReviewResult(
                    accepted=True, ref_map=dialog.get_ref_map(),
                ))
            else:
                runner.submit_review(None)
        elif payload.kind == "tts":
            dialog = TtsReviewDialog(payload.segments, parent=self)
            if dialog.exec() == QDialog.DialogCode.Accepted:
                runner.submit_review(ReviewResult(
                    accepted=True, regenerate_indices=dialog.get_regenerate_indices(),
                ))
            else:
                runner.submit_review(None)

    # ------------------------------------------------------------------ 窗口关闭

    def closeEvent(self, event) -> None:
        """关闭窗口前停止流水线线程，避免 QThread 销毁崩溃。"""
        runner = self._runner
        if runner is not None and runner.isRunning():
            runner.cancel()
            runner.wait(10000)
        super().closeEvent(event)

    # ------------------------------------------------------------------ 设置窗口

    def _open_settings_dialog(self) -> None:
        """打开统一设置窗口（资源管理 + 界面语言）。"""
        dialog = SettingsDialog(self._manager, self)
        # 用 lambda 转发日志：语言切换重建后仍指向最新的日志面板
        dialog.log_requested.connect(lambda message: self._pipeline_panel.log(message))
        dialog.language_changed.connect(self._on_language_changed)
        dialog.exec()

    def _on_language_changed(self, _code: str) -> None:
        """界面语言已切换：整体重建界面（用户状态自动保留）。"""
        self._build_ui()
