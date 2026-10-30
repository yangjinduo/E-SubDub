"""统一设置窗口：界面语言 / 主题切换 + 运行环境搭建 + 全部资源管理
+ 任务运行期间防休眠开关。

替代旧的"单资源设置对话框"：一个入口查看各资源的准备状态与本地路径，
并直接执行删除（后台线程彻底删除，不进回收站）与下载（含进度与取消）。

"运行环境"行位于资源列表最上方（音频提取之上）：检测 TTS 独立虚拟环境
（.venv-tts）与 llama-server 是否就绪，未就绪时一键搭建（后台 QThread，
含步骤进度与取消；完成后资源栏的推理设备卡同步刷新）；已部署时提供
删除按钮（后台线程彻底删除，重新部署即重建）。

TTS 的三个辅助模型（w2v-bert-2.0 / CAM++ / BigVGAN v2）不单独出资源
行：并入"声音克隆"行（模型名显示"IndexTTS-2.5 及辅助模型"），点击
下载即并行补齐缺失的主权重与辅助模型，取消一并取消，徽标显示整组
平均进度；三者全部就位后"声音克隆"才显示已就绪。

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
    QCheckBox,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QApplication,
    QVBoxLayout,
    QWidget,
)

from app.core import sleep_guard
from app.core.hardware import DeviceInfo, detect_device_standalone
from app.core.i18n import SUPPORTED_LANGUAGES, get_language, set_language, tr
from app.resources.deploy_log import deploy_log_path
from app.resources.downloader import get_model_downloader
from app.resources.env_setup import (
    environment_status,
    get_env_setup_manager,
    runtime_env_components,
)
from app.resources.model import (
    RESOURCE_META,
    TTS_AUX_KEYS,
    ResourceInfo,
    ResourceManager,
    get_models_root,
)
from app.ui.style import (
    SUPPORTED_THEMES,
    apply_theme,
    get_theme,
    set_theme,
    tokens,
)

# "声音克隆"行的展示分组：主权重 + 辅助模型三件套（一行聚合展示与操作）
_TTS_GROUP: tuple[str, ...] = ("tts",) + TTS_AUX_KEYS


class SettingsDialog(QDialog):
    """统一设置窗口（运行环境 + 资源管理 + 界面语言与主题）。

    信号：
        log_requested(str): 请求主窗口日志区追加一行
        language_changed(str): 界面语言已切换，参数为新语言代码（主窗口据此重建 UI）
        theme_changed(str): 界面主题已切换，参数为新主题代码（主窗口据此重建 UI）
    """

    log_requested = Signal(str)
    language_changed = Signal(str)
    theme_changed = Signal(str)

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
        # 各下载的最近进度（0-1），供 tts 行聚合整组（主权重 + 辅助模型）进度
        self._download_pct: dict[str, float] = {}
        # 后台删除进行中的资源 key（行内按钮显示"删除中…"并禁用）
        self._deleting_keys: set[str] = set()
        # 运行环境删除进行中
        self._env_deleting = False

        self._build_ui()

        # 资源状态变化（下载完成 / 删除）时刷新对应行
        self._manager.resource_status_changed.connect(self._on_status_changed)
        # 后台删除结束（成功 / 失败）：记录日志并恢复行的初始状态
        self._manager.resource_delete_finished.connect(self._on_resource_deleted)
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
        # 运行环境后台删除结果
        self._env_mgr.delete_finished.connect(self._on_env_delete_finished)

    # ------------------------------------------------------------------ 构建

    def _build_ui(self) -> None:  # noqa: C901 - 多为布局代码
        self.setWindowTitle(tr("common.settings"))
        self.setMinimumWidth(780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # ---- 界面语言 + 界面主题（同一行：语言在左，主题在其右侧） ----
        # 标签与选项名双语写死（不随界面语言翻译）：无论用户懂哪种语言，
        # 都能认出此处修改界面语言 / 主题（深浅色固定软件自身配色，
        # 不跟随系统深浅色）
        pref_row = QHBoxLayout()
        pref_row.setSpacing(10)
        self._lang_label = QLabel("语言 / Language")
        self._lang_combo = QComboBox()
        for code, native in SUPPORTED_LANGUAGES.items():
            self._lang_combo.addItem(native, code)
        self._lang_combo.setCurrentIndex(
            list(SUPPORTED_LANGUAGES.keys()).index(get_language())
        )
        self._lang_combo.currentIndexChanged.connect(self._on_language_changed)
        pref_row.addWidget(self._lang_label)
        pref_row.addWidget(self._lang_combo)
        pref_row.addSpacing(24)
        self._theme_label = QLabel("主题 / Theme")
        self._theme_combo = QComboBox()
        for code, native in SUPPORTED_THEMES.items():
            self._theme_combo.addItem(native, code)
        self._theme_combo.setCurrentIndex(
            list(SUPPORTED_THEMES.keys()).index(get_theme())
        )
        self._theme_combo.currentIndexChanged.connect(self._on_theme_changed)
        pref_row.addWidget(self._theme_label)
        pref_row.addWidget(self._theme_combo)
        pref_row.addStretch(1)
        layout.addLayout(pref_row)

        # ---- 有任务 / 下载运行时禁止系统休眠（防止长任务被休眠中断） ----
        self._sleep_check = QCheckBox(tr("settings.prevent_sleep"))
        self._sleep_check.setToolTip(tr("settings.prevent_sleep_tooltip"))
        self._sleep_check.setChecked(sleep_guard.is_enabled())
        self._sleep_check.toggled.connect(self._on_sleep_toggled)
        layout.addWidget(self._sleep_check)

        # 分隔线
        separator = QLabel()
        separator.setFixedHeight(1)
        separator.setStyleSheet(f"background: {tokens()['separator_line']};")
        self._separator = separator
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
        """按管理器当前状态重建资源行（运行环境行置顶，辅助模型不单独出行）。"""
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

        for row, info in enumerate(self._manager.visible_resources(), start=1):
            # 列 0：功能名 + 模型名（声音克隆行标注"及辅助模型"）
            name_column = QWidget()
            name_layout = QVBoxLayout(name_column)
            name_layout.setContentsMargins(0, 0, 0, 0)
            name_layout.setSpacing(1)
            c = tokens()
            feature = QLabel(tr(f"resource.{info.key}.feature"))
            feature.setStyleSheet(
                f"font-size: 13px; font-weight: 600; color: {c['feature_name']};"
            )
            model_text = info.model_name
            if info.key == "tts":
                model_text = f"{info.model_name} {tr('resource.tts.aux_suffix')}"
            model = QLabel(model_text)
            model.setStyleSheet(f"font-size: 12px; color: {c['model_name']};")
            name_layout.addWidget(feature)
            name_layout.addWidget(model)
            grid.addWidget(name_column, row, 0)

            # 列 1：状态徽标（下载中显示整组进度，全部完成才显示"已就绪"）
            downloading = self._row_downloading(info.key)
            badge = QLabel()
            if downloading:
                badge.setObjectName("notReadyBadge")
                badge.setText(f"{self._group_progress(info.key) * 100:.0f}%")
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

    # ------------------------------------------------------------------ 行分组

    def _group_keys(self, key: str) -> tuple[str, ...]:
        """资源行的操作分组：tts 行聚合主权重与辅助模型三件套，其余单行单资源。"""
        return _TTS_GROUP if key == "tts" else (key,)

    def _row_key(self, key: str) -> str:
        """下载 key -> 所属行 key（辅助模型的进度 / 完成事件归到 tts 行）。"""
        return "tts" if key in TTS_AUX_KEYS else key

    def _row_downloading(self, key: str) -> bool:
        """该行是否有任一资源处于下载中（tts 行看整组）。"""
        return any(self._downloader.is_downloading(k) for k in self._group_keys(key))

    def _group_progress(self, key: str) -> float:
        """该行整组下载进度（0-1）：下载中取最近进度，已就绪计满，其余计零。"""
        parts = []
        for k in self._group_keys(key):
            if self._downloader.is_downloading(k):
                parts.append(self._download_pct.get(k, 0.0))
            elif self._manager.get(k).status.is_ready:
                parts.append(1.0)
            else:
                parts.append(0.0)
        return sum(parts) / len(parts) if parts else 0.0

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
        c = tokens()
        feature = QLabel(tr("settings.env_feature"))
        feature.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {c['feature_name']};"
        )
        sub = QLabel(tr("settings.env_sub"))
        sub.setStyleSheet(f"font-size: 12px; color: {c['model_name']};")
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
            progress.setStyleSheet(f"color: {c['accent_text']};")
            self._env_refs["progress"] = progress
            layout.addWidget(progress)
        elif both_ready:
            ready = QLabel(tr("settings.env_ready_detail"))
            ready.setStyleSheet(f"color: {c['model_name']}; font-size: 12px;")
            layout.addWidget(ready)
        else:
            missing = QLabel(tr("settings.env_missing_detail"))
            missing.setStyleSheet(f"color: {c['text_body']};")
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
        elif self._env_deleting:
            deleting_button = QPushButton(tr("settings.deleting"))
            deleting_button.setEnabled(False)
            actions_layout.addWidget(deleting_button)
        else:
            if not both_ready:
                setup_button = QPushButton(tr("settings.env_setup"))
                setup_button.clicked.connect(self._on_env_setup_clicked)
                actions_layout.addWidget(setup_button)
            # 有已部署组件（.venv-tts / tools / third_party 等）时提供删除，
            # 与模型行的删除一致：后台线程彻底删除，重新部署即重建
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
        """清除已部署的运行环境（确认后后台线程彻底删除，可重新一键部署）。

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
        if self._env_mgr.start_delete():
            self._env_deleting = True
            self._rebuild_resource_rows()

    def _on_env_delete_finished(self, ok: bool, detail: str) -> None:
        """运行环境后台删除结束：日志 + 失败弹窗 + 刷新行与设备卡徽标。

        detail 为已删除的组件名列表（成功时）或错误文案（失败时）；
        成功但列表为空 = 本机本就没有可删除内容。
        """
        self._env_deleting = False
        if ok:
            if detail:
                self.log_requested.emit(tr("settings.env_delete_done"))
            else:
                self.log_requested.emit(tr("settings.env_delete_nothing"))
        else:
            msg = detail
            path = deploy_log_path()
            if path is not None:
                msg += f"\n{tr('common.log_hint', path=path)}"
            self.log_requested.emit(tr("settings.env_delete_failed", error=msg))
            QMessageBox.warning(self, tr("common.settings"), msg)
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
        """单个资源是否处于下载中。"""
        return self._downloader.is_downloading(key)

    def _build_location_cell(self, info: ResourceInfo) -> QWidget:
        """位置列：下载进度 / 就绪路径 / 需下载体积 / 内置提示。"""
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        c = tokens()

        if self._row_downloading(info.key):
            pct = f"{self._group_progress(info.key) * 100:.0f}"
            progress = QLabel(tr("settings.downloading", pct=pct, speed="…"))
            progress.setStyleSheet(f"color: {c['accent_text']};")
            self._row_refs.setdefault(info.key, {})["progress"] = progress
            layout.addWidget(progress)
        elif info.status.is_ready:
            path_edit = QLineEdit(info.local_path or tr("settings.path_unknown"))
            path_edit.setReadOnly(True)
            path_edit.setMinimumHeight(30)
            layout.addWidget(path_edit)
        elif info.key == "ffmpeg":
            hint = QLabel(tr("settings.builtin_hint"))
            hint.setStyleSheet(f"color: {c['text_faint']}; font-size: 12px;")
            layout.addWidget(hint)
        else:
            # 需下载体积：tts 行合并辅助模型三件套（一并自动下载）
            size_gb = info.download_size_gb
            if info.key == "tts":
                size_gb += sum(
                    RESOURCE_META[k].download_size_gb for k in TTS_AUX_KEYS
                )
            need = QLabel(tr("settings.need_download", size=f"{size_gb:g}"))
            need.setStyleSheet(f"color: {c['text_body']};")
            layout.addWidget(need)
        return cell

    def _build_actions_cell(self, info: ResourceInfo) -> QWidget:
        """操作列：下载 / 取消 / 删除 / 模型主页。"""
        cell = QWidget()
        layout = QHBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        if self._row_downloading(info.key):
            cancel_button = QPushButton(tr("common.cancel"))
            cancel_button.clicked.connect(lambda _=False, k=info.key: self._on_cancel_clicked(k))
            layout.addWidget(cancel_button)
        elif info.status.is_ready:
            if info.key == "ffmpeg":
                builtin = QLabel(tr("settings.builtin"))
                builtin.setStyleSheet(f"color: {tokens()['text_faint']}; font-size: 12px;")
                layout.addWidget(builtin)
            elif info.key in self._deleting_keys:
                deleting_button = QPushButton(tr("settings.deleting"))
                deleting_button.setEnabled(False)
                layout.addWidget(deleting_button)
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

    # ------------------------------------------------------------------ 语言 / 主题

    def _on_language_changed(self, index: int) -> None:
        """切换界面语言：立即生效并通知主窗口重建。"""
        code = self._lang_combo.itemData(index)
        if code is None or code == get_language():
            return
        set_language(code)
        self._retranslate()
        self.language_changed.emit(code)

    def _on_theme_changed(self, index: int) -> None:
        """切换界面主题：立即应用到整个应用并通知主窗口重建。

        浅色 / 深色均为软件内置配色（Fusion 样式 + 显式调色板），
        切换与运行全程不受 Windows 系统深浅色模式影响。
        """
        code = self._theme_combo.itemData(index)
        if code is None or code == get_theme():
            return
        set_theme(code)
        app = QApplication.instance()
        if app is not None:
            apply_theme(app)
        self._refresh_static_styles()
        self._rebuild_resource_rows()
        self.theme_changed.emit(code)

    def _refresh_static_styles(self) -> None:
        """主题切换后重取本窗口静态控件的行内样式颜色。"""
        c = tokens()
        self._separator.setStyleSheet(f"background: {c['separator_line']};")

    def _retranslate(self) -> None:
        """语言切换后刷新本窗口内的静态文案（语言标签双语写死无需刷新；资源行整体重建）。"""
        self.setWindowTitle(tr("common.settings"))
        self._resources_title.setText(tr("settings.resources"))
        self._deploy_all_button.setText(tr("settings.deploy_all"))
        self._sleep_check.setText(tr("settings.prevent_sleep"))
        self._sleep_check.setToolTip(tr("settings.prevent_sleep_tooltip"))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 防休眠

    def _on_sleep_toggled(self, checked: bool) -> None:
        """切换「任务 / 下载运行时禁止系统休眠」：立即生效并持久化。

        实际是否阻止休眠由 sleep_guard 按（开关 × 当前有无活动）决定，
        详见 app/core/sleep_guard.py。
        """
        sleep_guard.set_enabled(checked)

    # ------------------------------------------------------------------ 下载

    def _start_default_download(self, key: str) -> bool:
        """按默认位置（软件安装目录下 models/<默认子目录>）启动后台下载。

        返回是否真正发起了下载（已就绪或在下载中的资源跳过）。
        """
        info = self._manager.get(key)
        meta = RESOURCE_META.get(key)
        if meta is None or not meta.repo_id:
            # 内置资源（如 FFmpeg）不提供下载
            return False
        if info.status.is_ready or self._is_downloading(key):
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
        """点击下载：直接下载到默认位置（软件安装目录）。

        "声音克隆"行一并启动主权重与辅助模型三件套中缺失项的下载
        （已就绪 / 在下载中的自动跳过）。
        """
        for k in self._group_keys(key):
            self._start_default_download(k)
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
        """取消正在进行的下载（已完成文件保留，重新下载可续传）。

        "声音克隆"行一并取消主权重与辅助模型三件套的全部下载。
        """
        for k in self._group_keys(key):
            if self._downloader.is_downloading(k):
                self._downloader.cancel(k)
        self._rebuild_resource_rows()

    def _on_download_progress(self, key: str, percent: float, speed: str) -> None:
        """更新对应行的下载进度文案与状态徽标百分比。

        辅助模型的进度归并到"声音克隆"行，徽标显示整组（主权重 +
        辅助模型三件套）平均进度；进度文案显示当前回报的下载速度。
        """
        self._download_pct[key] = percent
        row_key = self._row_key(key)
        refs = self._row_refs.get(row_key)
        if refs is None:
            return
        pct_value = f"{self._group_progress(row_key) * 100:.0f}"
        progress = refs.get("progress")
        if progress is not None:
            progress.setText(
                tr("settings.downloading", pct=pct_value, speed=speed)
            )
        # 状态徽标同步显示整组进度（下载完成前不显示"已就绪"）
        badge = refs.get("badge")
        if badge is not None:
            badge.setText(f"{pct_value}%")

    def _on_download_finished(self, key: str, local_path: str) -> None:
        """下载完成：下载一律走默认位置，按检测结果刷新状态。"""
        info = self._manager.get(key)
        self._download_pct.pop(key, None)
        self._manager.refresh(key)
        # 辅助模型就位情况影响"声音克隆"行的就绪判定（_detect_tts 要求
        # 三件套齐全），需连带刷新主资源
        if key in TTS_AUX_KEYS:
            self._manager.refresh("tts")
        self.log_requested.emit(tr("settings.download_done", name=info.model_name))
        self._rebuild_resource_rows()

    def _on_download_failed(self, key: str, error: str) -> None:
        """下载失败：弹窗提示并恢复行的初始状态。"""
        info = self._manager.get(key)
        self._download_pct.pop(key, None)
        self._manager.refresh(key)
        if key in TTS_AUX_KEYS:
            self._manager.refresh("tts")
        self.log_requested.emit(tr("settings.download_failed", name=info.model_name, error=error))
        QMessageBox.warning(
            self, tr("common.settings"), tr("settings.download_failed", name=info.model_name, error=error)
        )
        self._rebuild_resource_rows()

    def _on_download_cancelled(self, key: str) -> None:
        """下载被用户取消：记录日志并恢复行的初始状态。"""
        info = self._manager.get(key)
        self._download_pct.pop(key, None)
        self._manager.refresh(key)
        if key in TTS_AUX_KEYS:
            self._manager.refresh("tts")
        self.log_requested.emit(tr("settings.download_cancelled", name=info.model_name))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 删除

    def _on_delete_clicked(self, key: str) -> None:
        """删除本地模型文件（确认后后台线程彻底删除，不进回收站）。"""
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
        if self._manager.delete_resource(key):
            self._deleting_keys.add(key)
            self._rebuild_resource_rows()
        else:
            self.log_requested.emit(tr("settings.delete_nothing", name=info.model_name))
            self._rebuild_resource_rows()

    def _on_resource_deleted(self, key: str, deleted: bool) -> None:
        """后台删除结束：记录日志并恢复行的初始状态（状态刷新由管理器完成）。"""
        self._deleting_keys.discard(key)
        info = self._manager.get(key)
        if deleted:
            self.log_requested.emit(tr("settings.delete_done", name=info.model_name))
        else:
            self.log_requested.emit(tr("settings.delete_nothing", name=info.model_name))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 状态

    def _on_status_changed(self, key: str) -> None:
        """资源状态变化时重建行（下载完成 / 删除后由管理器触发）。"""
        self._rebuild_resource_rows()
