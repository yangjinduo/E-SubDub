"""统一设置窗口：界面语言切换 + 运行环境搭建 + 全部资源管理。

替代旧的"单资源设置对话框"：一个入口查看五项资源的准备状态与本地路径，
并直接执行删除（移入 .trash）与下载（含进度与取消）。

"运行环境"行位于资源列表最上方（音频提取之上）：检测 TTS 独立虚拟环境
（.venv-tts）与 llama-server 是否就绪，未就绪时一键搭建（后台 QThread，
含步骤进度与取消；完成后资源栏的推理设备卡同步刷新）；已部署时提供
删除按钮（各组件移入 .trash 可找回，与模型删除一致）。

"资源管理"标题行右侧提供"一键部署"按钮：后台并行搭建运行环境与下载
全部缺失模型（每资源独立下载线程，互不阻塞；环境搭建线程同步进行）。
下载一律使用软件安装目录下的默认位置，不再让用户选择目标文件夹；
下载中的资源在状态徽标处显示百分比，完成后才显示"已就绪"。

下载使用全局 ModelDownloader 单例，关闭本窗口后下载仍可在后台继续，
完成后资源状态栏卡片自动刷新。
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtCore import QUrl, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.hardware import DeviceInfo, detect_device_standalone
from app.core.i18n import SUPPORTED_LANGUAGES, get_language, set_language, tr
from app.resources.deploy_log import deploy_log_path
from app.resources.downloader import get_model_downloader
from app.resources.env_setup import (
    delete_runtime_env,
    environment_status,
    get_env_setup_manager,
    runtime_env_components,
)
from app.resources.model import (
    RESOURCE_META,
    ResourceInfo,
    ResourceManager,
    get_models_root,
)


class SettingsDialog(QDialog):
    """统一设置窗口（运行环境 + 资源管理 + 界面语言）。

    信号：
        log_requested(str): 请求主窗口日志区追加一行
        language_changed(str): 界面语言已切换，参数为新语言代码（主窗口据此重建 UI）
    """

    log_requested = Signal(str)
    language_changed = Signal(str)

    def __init__(
        self,
        manager: ResourceManager,
        device: DeviceInfo | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._manager = manager
        self._device = device or detect_device_standalone()
        self._downloader = get_model_downloader()
        self._env_mgr = get_env_setup_manager()
        # 每行需要动态更新的控件引用：key -> {"progress": QLabel}
        self._row_refs: dict[str, dict] = {}
        # 运行环境行的进度控件引用（搭建进行中时存在）
        self._env_refs: dict[str, QLabel] = {}
        self._rows_host: QWidget | None = None

        self._build_ui()

        # 资源状态变化（下载完成 / 删除）时刷新对应行
        self._manager.resource_status_changed.connect(self._on_status_changed)
        # 下载进度 / 结果
        self._downloader.download_progress.connect(self._on_download_progress)
        self._downloader.download_finished.connect(self._on_download_finished)
        self._downloader.download_failed.connect(self._on_download_failed)
        self._downloader.download_cancelled.connect(self._on_download_cancelled)
        # 环境搭建进度 / 结果
        self._env_mgr.setup_started.connect(self._on_env_started)
        self._env_mgr.setup_progress.connect(self._on_env_progress)
        self._env_mgr.setup_finished.connect(self._on_env_finished)
        self._env_mgr.setup_cancelled.connect(self._on_env_cancelled)

    # ------------------------------------------------------------------ 构建

    def _build_ui(self) -> None:  # noqa: C901 - 多为布局代码
        self.setWindowTitle(tr("common.settings"))
        self.setMinimumWidth(780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # ---- 界面语言 ----
        lang_row = QHBoxLayout()
        lang_row.setSpacing(10)
        # 标签双语写死（不随界面语言翻译）：无论用户懂哪种语言，都能认出此处修改界面语言
        self._lang_label = QLabel("语言 / Language")
        self._lang_combo = QComboBox()
        for code, native in SUPPORTED_LANGUAGES.items():
            self._lang_combo.addItem(native, code)
        self._lang_combo.setCurrentIndex(
            list(SUPPORTED_LANGUAGES.keys()).index(get_language())
        )
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        self._lang_hint = QLabel(tr("settings.language_hint"))
        self._lang_hint.setStyleSheet("color: #9CA3AF; font-size: 12px;")
        lang_row.addWidget(self._lang_label)
        lang_row.addWidget(self._lang_combo)
        lang_row.addWidget(self._lang_hint)
        lang_row.addStretch(1)
        layout.addLayout(lang_row)

        # 分隔线
        separator = QLabel()
        separator.setFixedHeight(1)
        separator.setStyleSheet("background: #E5E7EB;")
        layout.addWidget(separator)

        # ---- 资源管理（标题行右侧：一键部署） ----
        resources_header = QHBoxLayout()
        resources_header.setSpacing(10)
        self._resources_title = QLabel(tr("settings.resources"))
        self._resources_title.setObjectName("sectionTitle")
        resources_header.addWidget(self._resources_title)
        resources_header.addStretch(1)
        self._deploy_all_button = QPushButton(tr("settings.deploy_all"))
        self._deploy_all_button.setObjectName("primaryButton")
        self._deploy_all_button.clicked.connect(self._on_deploy_all_clicked)
        resources_header.addWidget(self._deploy_all_button)
        layout.addLayout(resources_header)

        # 资源行容器（状态变化时整体重建）
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(8)
        layout.addLayout(self._rows_layout)
        layout.addStretch(1)

        # 底部关闭按钮
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        close_button = QPushButton(tr("common.close"))
        close_button.clicked.connect(self.reject)
        button_row.addWidget(close_button)
        layout.addLayout(button_row)

        self._rebuild_resource_rows()

    def _rebuild_resource_rows(self) -> None:
        """按管理器当前状态重建全部资源行（运行环境行置顶）。"""
        if self._rows_host is not None:
            self._rows_layout.removeWidget(self._rows_host)
            self._rows_host.deleteLater()
            self._rows_host = None
        self._row_refs.clear()
        self._env_refs.clear()

        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(2, 1)

        # 第 0 行：运行环境（TTS 虚拟环境 + LLAMA 服务），位于音频提取之上
        self._build_env_row(grid)

        for row, info in enumerate(self._manager.resources(), start=1):
            # 列 0：功能名 + 模型名
            name_column = QWidget()
            name_layout = QVBoxLayout(name_column)
            name_layout.setContentsMargins(0, 0, 0, 0)
            name_layout.setSpacing(1)
            feature = QLabel(tr(f"resource.{info.key}.feature"))
            feature.setStyleSheet("font-size: 13px; font-weight: 600; color: #111827;")
            model = QLabel(info.model_name)
            model.setStyleSheet("font-size: 12px; color: #6B7280;")
            name_layout.addWidget(feature)
            name_layout.addWidget(model)
            grid.addWidget(name_column, row, 0)

            # 列 1：状态徽标（下载中显示百分比，下载完成才显示"已就绪"）
            downloading = self._is_downloading(info.key)
            badge = QLabel()
            if downloading:
                badge.setObjectName("notReadyBadge")
                badge.setText("0%")
            else:
                badge.setObjectName("readyBadge" if info.status.is_ready else "notReadyBadge")
                badge.setText(tr("common.ready") if info.status.is_ready else tr("common.not_ready"))
            self._row_refs[info.key] = {"badge": badge}
            grid.addWidget(badge, row, 1)

            # 列 2：位置 / 下载信息
            grid.addWidget(self._build_location_cell(info), row, 2)

            # 列 3：操作按钮
            grid.addWidget(self._build_actions_cell(info), row, 3)

        self._rows_host = host
        self._rows_layout.addWidget(host)
        self._update_deploy_button_state()

    def _build_env_row(self, grid: QGridLayout) -> None:
        """运行环境行：检测状态 + 搭建按钮 / 搭建进度 + 取消。"""
        env = environment_status()
        running = self._env_mgr.is_running()
        both_ready = env["tts_ready"] and env["llama_ready"]

        # 列 0：名称 + 副标题
        name_column = QWidget()
        name_layout = QVBoxLayout(name_column)
        name_layout.setContentsMargins(0, 0, 0, 0)
        name_layout.setSpacing(1)
        feature = QLabel(tr("settings.env_feature"))
        feature.setStyleSheet("font-size: 13px; font-weight: 600; color: #111827;")
        sub = QLabel(tr("settings.env_sub"))
        sub.setStyleSheet("font-size: 12px; color: #6B7280;")
        name_layout.addWidget(feature)
        name_layout.addWidget(sub)
        grid.addWidget(name_column, 0, 0)

        # 列 1：状态徽标（tooltip 列出分项明细）
        badge = QLabel()
        badge.setObjectName("readyBadge" if both_ready else "notReadyBadge")
        badge.setText(tr("common.ready") if both_ready else tr("common.not_ready"))
        badge.setToolTip(
            f"{tr('bar.env_tts')}：{tr('common.ready' if env['tts_ready'] else 'common.not_ready')}\n"
            f"{tr('bar.env_llama')}：{tr('common.ready' if env['llama_ready'] else 'common.not_ready')}"
        )
        grid.addWidget(badge, 0, 1)

        # 列 2：状态明细 / 搭建进度
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        if running:
            progress = QLabel(tr("env.step.check_tts"))
            progress.setStyleSheet("color: #2563EB;")
            self._env_refs["progress"] = progress
            layout.addWidget(progress)
        elif both_ready:
            ready = QLabel(tr("settings.env_ready_detail"))
            ready.setStyleSheet("color: #6B7280; font-size: 12px;")
            layout.addWidget(ready)
        else:
            missing = QLabel(tr("settings.env_missing_detail"))
            missing.setStyleSheet("color: #374151;")
            layout.addWidget(missing)
        grid.addWidget(cell, 0, 2)

        # 列 3：操作（搭建 / 删除 / 取消）
        actions = QWidget()
        actions_layout = QHBoxLayout(actions)
        actions_layout.setContentsMargins(0, 0, 0, 0)
        actions_layout.setSpacing(8)
        if running:
            cancel_button = QPushButton(tr("common.cancel"))
            cancel_button.clicked.connect(self._env_mgr.cancel)
            actions_layout.addWidget(cancel_button)
        else:
            if not both_ready:
                setup_button = QPushButton(tr("settings.env_setup"))
                setup_button.clicked.connect(self._on_env_setup_clicked)
                actions_layout.addWidget(setup_button)
            # 有已部署组件（.venv-tts / tools / third_party 等）时提供删除，
            # 与模型行的删除一致：移入 .trash 可找回，重新部署即重建
            if runtime_env_components():
                delete_button = QPushButton(tr("common.delete"))
                delete_button.setObjectName("dangerButton")
                delete_button.clicked.connect(self._on_env_delete_clicked)
                actions_layout.addWidget(delete_button)
        actions_layout.addStretch(1)
        grid.addWidget(actions, 0, 3)

    def _on_env_setup_clicked(self) -> None:
        """点击"开始搭建"：后台补齐缺失的 TTS 虚拟环境 / llama-server。"""
        self._env_mgr.start_setup(self._device)

    def _on_env_delete_clicked(self) -> None:
        """清除已部署的运行环境（确认后各组件移入 .trash，可重新一键部署）。

        开发态清除只动 .venv-tts / tools / third_party 等部署产物，
        主开发环境（项目根 venv/）不受影响。
        """
        answer = QMessageBox.question(
            self,
            tr("settings.confirm_env_delete_title"),
            tr("settings.confirm_env_delete"),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            moved = delete_runtime_env()
        except RuntimeError as e:
            msg = str(e)
            path = deploy_log_path()
            if path is not None:
                msg += f"\n{tr('common.log_hint', path=path)}"
            self.log_requested.emit(tr("settings.env_delete_failed", error=msg))
            QMessageBox.warning(self, tr("common.settings"), msg)
        else:
            if moved:
                self.log_requested.emit(tr("settings.env_delete_done"))
            else:
                self.log_requested.emit(tr("settings.env_delete_nothing"))
            # 通知主窗口（设备卡等）运行环境就绪状态已变化
            self._env_mgr.environment_changed.emit()
        self._rebuild_resource_rows()

    def _on_env_started(self) -> None:
        """环境搭建开始：记录日志并刷新运行环境行（按钮 → 进度 + 取消）。"""
        self.log_requested.emit(tr("env.setup_started"))
        self._rebuild_resource_rows()

    def _on_env_progress(self, text: str) -> None:
        """环境搭建进度文案更新。"""
        progress = self._env_refs.get("progress")
        if progress is not None:
            progress.setText(text)

    def _on_env_finished(self, ok: bool, detail: str) -> None:
        """环境搭建结束：日志 + 失败弹窗 + 刷新运行环境行。"""
        self.log_requested.emit(detail)
        if not ok:
            QMessageBox.warning(self, tr("common.settings"), detail)
        self._rebuild_resource_rows()

    def _on_env_cancelled(self) -> None:
        """环境搭建被取消：记录日志并刷新运行环境行。"""
        self.log_requested.emit(tr("env.setup_cancelled"))
        self._rebuild_resource_rows()

    def _is_downloading(self, key: str) -> bool:
        """资源行是否处于下载中。"""
        return self._downloader.is_downloading(key)

    def _build_location_cell(self, info: ResourceInfo) -> QWidget:
        """位置列：下载进度 / 就绪路径 / 需下载体积 / 内置提示。"""
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)

        if self._is_downloading(info.key):
            progress = QLabel(tr("settings.downloading", pct="0", speed="…"))
            progress.setStyleSheet("color: #2563EB;")
            self._row_refs.setdefault(info.key, {})["progress"] = progress
            layout.addWidget(progress)
        elif info.status.is_ready:
            path_edit = QLineEdit(info.local_path or tr("settings.path_unknown"))
            path_edit.setReadOnly(True)
            path_edit.setMinimumHeight(30)
            layout.addWidget(path_edit)
        elif info.key == "ffmpeg":
            hint = QLabel(tr("settings.builtin_hint"))
            hint.setStyleSheet("color: #9CA3AF; font-size: 12px;")
            layout.addWidget(hint)
        else:
            size_text = f"{info.download_size_gb:g}"
            need = QLabel(tr("settings.need_download", size=size_text))
            need.setStyleSheet("color: #374151;")
            layout.addWidget(need)
        return cell

    def _build_actions_cell(self, info: ResourceInfo) -> QWidget:
        """操作列：下载 / 取消 / 删除 / 模型主页。"""
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if self._is_downloading(info.key):
            cancel_button = QPushButton(tr("common.cancel"))
            cancel_button.clicked.connect(lambda _=False, k=info.key: self._on_cancel_clicked(k))
            layout.addWidget(cancel_button)
        elif info.status.is_ready:
            if info.key == "ffmpeg":
                builtin = QLabel(tr("settings.builtin"))
                builtin.setStyleSheet("color: #9CA3AF; font-size: 12px;")
                layout.addWidget(builtin)
            else:
                delete_button = QPushButton(tr("common.delete"))
                delete_button.setObjectName("dangerButton")
                delete_button.clicked.connect(lambda _=False, k=info.key: self._on_delete_clicked(k))
                layout.addWidget(delete_button)
        elif info.key != "ffmpeg":
            download_button = QPushButton(tr("common.download"))
            download_button.clicked.connect(lambda _=False, k=info.key: self._on_download_clicked(k))
            layout.addWidget(download_button)
            if info.homepage_url:
                homepage_button = QPushButton(tr("common.model_homepage"))
                homepage_button.setObjectName("linkButton")
                homepage_button.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
                homepage_button.clicked.connect(
                    lambda _=False, url=info.homepage_url: QDesktopServices.openUrl(QUrl(url))
                )
                layout.addWidget(homepage_button)

        layout.addStretch(1)
        return cell

    # ------------------------------------------------------------------ 语言

    def _on_language_changed(self, index: int) -> None:
        """切换界面语言：立即生效并通知主窗口重建。"""
        code = self._lang_combo.itemData(index)
        if code is None or code == get_language():
            return
        set_language(code)
        self._retranslate()
        self.language_changed.emit(code)

    def _retranslate(self) -> None:
        """语言切换后刷新本窗口内的静态文案（语言标签双语写死无需刷新；资源行整体重建）。"""
        self.setWindowTitle(tr("common.settings"))
        self._lang_hint.setText(tr("settings.language_hint"))
        self._resources_title.setText(tr("settings.resources"))
        self._deploy_all_button.setText(tr("settings.deploy_all"))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 下载

    def _start_default_download(self, key: str) -> bool:
        """按默认位置（软件安装目录下 models/<默认子目录>）启动后台下载。

        返回是否真正发起了下载（已在下载中的资源跳过）。
        """
        info = self._manager.get(key)
        meta = RESOURCE_META.get(key)
        if meta is None or not meta.repo_id:
            # 内置资源（如 FFmpeg）不提供下载
            return False
        if self._is_downloading(key):
            return False
        self._downloader.start_download(
            key=key,
            repo_id=meta.repo_id,
            target_dir=str(get_models_root() / meta.default_subdir),
            allow_patterns=meta.allow_patterns,
            source=meta.source,
        )
        self.log_requested.emit(tr("settings.download_started", name=info.model_name))
        return True

    def _on_download_clicked(self, key: str) -> None:
        """点击下载：直接下载到默认位置（软件安装目录），不再让用户选择目标。"""
        self._start_default_download(key)
        self._rebuild_resource_rows()

    def _on_deploy_all_clicked(self) -> None:
        """一键部署：并行搭建运行环境 + 下载全部缺失模型（目标为软件安装目录）。

        环境搭建一个后台线程、每个模型一个独立下载线程，互不阻塞
        （支持多任务并行）；已在进行的任务自动跳过（幂等，可重复点击
        补齐漏项）。
        """
        started_any = False
        running_any = False
        env = environment_status()
        if not (env["tts_ready"] and env["llama_ready"] and env["ml_ready"]):
            if self._env_mgr.is_running():
                running_any = True
            else:
                self._env_mgr.start_setup(self._device)
                started_any = True
        if self._env_mgr.is_running():
            running_any = True
        for info in self._manager.resources():
            if info.status.is_ready:
                continue
            if self._is_downloading(info.key):
                running_any = True
                continue
            if self._start_default_download(info.key):
                started_any = True
        if started_any:
            self.log_requested.emit(tr("settings.deploy_all_started"))
        elif running_any:
            self.log_requested.emit(tr("settings.deploy_all_running"))
        else:
            self.log_requested.emit(tr("settings.deploy_all_nothing"))
        self._rebuild_resource_rows()

    def _update_deploy_button_state(self) -> None:
        """一键部署按钮：运行环境与全部模型均已就绪（无可部署项）时禁用。"""
        if getattr(self, "_deploy_all_button", None) is None:
            return
        env = environment_status()
        env_pending = not (env["tts_ready"] and env["llama_ready"] and env["ml_ready"])
        model_pending = any(
            meta is not None and bool(meta.repo_id) and not info.status.is_ready
            for info in self._manager.resources()
            for meta in (RESOURCE_META.get(info.key),)
        )
        self._deploy_all_button.setEnabled(env_pending or model_pending)

    def _on_cancel_clicked(self, key: str) -> None:
        """取消正在进行的下载（已完成文件保留，重新下载可续传）。"""
        self._downloader.cancel(key)
        self._rebuild_resource_rows()

    def _on_download_progress(self, key: str, percent: float, speed: str) -> None:
        """更新对应行的下载进度文案与状态徽标百分比。"""
        refs = self._row_refs.get(key)
        if refs is None:
            return
        pct_text = f"{percent * 100:.0f}%"
        progress = refs.get("progress")
        if progress is not None:
            progress.setText(
                tr("settings.downloading", pct=f"{percent * 100:.0f}", speed=speed)
            )
        # 状态徽标同步显示百分比（下载完成前不显示"已就绪"）
        badge = refs.get("badge")
        if badge is not None:
            badge.setText(pct_text)

    def _on_download_finished(self, key: str, local_path: str) -> None:
        """下载完成：下载一律走默认位置，按检测结果刷新状态。"""
        info = self._manager.get(key)
        self._manager.refresh(key)
        self.log_requested.emit(tr("settings.download_done", name=info.model_name))
        self._rebuild_resource_rows()

    def _on_download_failed(self, key: str, error: str) -> None:
        """下载失败：弹窗提示并恢复行的初始状态。"""
        info = self._manager.get(key)
        self.log_requested.emit(tr("settings.download_failed", name=info.model_name, error=error))
        QMessageBox.warning(
            self, tr("common.settings"), tr("settings.download_failed", name=info.model_name, error=error)
        )
        self._rebuild_resource_rows()

    def _on_download_cancelled(self, key: str) -> None:
        """下载被用户取消：记录日志并恢复行的初始状态。"""
        info = self._manager.get(key)
        self.log_requested.emit(tr("settings.download_cancelled", name=info.model_name))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 删除

    def _on_delete_clicked(self, key: str) -> None:
        """删除本地模型文件（确认后移入 .trash，可找回）。"""
        info = self._manager.get(key)
        if key == "ffmpeg":
            self.log_requested.emit(tr("settings.delete_builtin"))
            return
        answer = QMessageBox.question(
            self,
            tr("settings.confirm_delete_title"),
            tr("settings.confirm_delete", name=info.model_name),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        deleted = self._manager.delete_resource(key)
        if deleted:
            self.log_requested.emit(tr("settings.delete_done", name=info.model_name))
        else:
            self.log_requested.emit(tr("settings.delete_nothing", name=info.model_name))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 状态

    def _on_status_changed(self, key: str) -> None:
        """资源状态变化时重建行（下载完成 / 删除后由管理器触发）。"""
        self._rebuild_resource_rows()
