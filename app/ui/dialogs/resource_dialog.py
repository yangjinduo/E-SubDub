"""统一设置窗口：界面语言切换 + 全部资源管理。

替代旧的"单资源设置对话框"：一个入口查看五项资源的准备状态与本地路径，
并直接执行删除（移入 .trash）与下载（魔搭 snapshot_download，含进度与取消）。

下载使用全局 ModelDownloader 单例，关闭本窗口后下载仍可在后台继续，
完成后资源状态栏卡片自动刷新。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtGui import QCursor, QDesktopServices
from PySide6.QtCore import QUrl, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.core.i18n import SUPPORTED_LANGUAGES, get_language, set_language, tr
from app.resources.downloader import get_model_downloader
from app.resources.model import (
    RESOURCE_META,
    ResourceInfo,
    ResourceManager,
    ResourceStatus,
    get_models_root,
)


class SettingsDialog(QDialog):
    """统一设置窗口（资源管理 + 界面语言）。

    信号：
        log_requested(str): 请求主窗口日志区追加一行
        language_changed(str): 界面语言已切换，参数为新语言代码（主窗口据此重建 UI）
    """

    log_requested = Signal(str)
    language_changed = Signal(str)

    def __init__(self, manager: ResourceManager, parent=None) -> None:
        super().__init__(parent)
        self._manager = manager
        self._downloader = get_model_downloader()
        # 每行需要动态更新的控件引用：key -> {"progress": QLabel}
        self._row_refs: dict[str, dict] = {}
        self._rows_host: QWidget | None = None

        self._build_ui()

        # 资源状态变化（下载完成 / 删除）时刷新对应行
        self._manager.resource_status_changed.connect(self._on_status_changed)
        # 下载进度 / 结果
        self._downloader.download_progress.connect(self._on_download_progress)
        self._downloader.download_finished.connect(self._on_download_finished)
        self._downloader.download_failed.connect(self._on_download_failed)
        self._downloader.download_cancelled.connect(self._on_download_cancelled)

    # ------------------------------------------------------------------ 构建

    def _build_ui(self) -> None:  # noqa: C901 - 多为布局代码
        self.setWindowTitle(tr("settings.title"))
        self.setMinimumWidth(780)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 20)
        layout.setSpacing(12)

        # ---- 界面语言 ----
        lang_row = QHBoxLayout()
        lang_row.setSpacing(10)
        self._lang_label = QLabel(tr("settings.language"))
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

        # ---- 资源管理 ----
        self._resources_title = QLabel(tr("settings.resources"))
        self._resources_title.setObjectName("sectionTitle")
        layout.addWidget(self._resources_title)

        header = QGridLayout()
        header.setHorizontalSpacing(12)
        self._header_labels = [
            QLabel(tr("settings.col_function")),
            QLabel(tr("settings.col_status")),
            QLabel(tr("settings.col_location")),
            QLabel(tr("settings.col_actions")),
        ]
        for label in self._header_labels:
            label.setStyleSheet("color: #6B7280; font-size: 12px; font-weight: 600;")
        header.addWidget(self._header_labels[0], 0, 0)
        header.addWidget(self._header_labels[1], 0, 1)
        header.addWidget(self._header_labels[2], 0, 2)
        header.addWidget(self._header_labels[3], 0, 3)
        header.setColumnStretch(2, 1)
        layout.addLayout(header)

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
        """按管理器当前状态重建全部资源行。"""
        if self._rows_host is not None:
            self._rows_layout.removeWidget(self._rows_host)
            self._rows_host.deleteLater()
            self._rows_host = None
        self._row_refs.clear()

        host = QWidget()
        grid = QGridLayout(host)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(8)
        grid.setColumnStretch(2, 1)

        for row, info in enumerate(self._manager.resources()):
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

            # 列 1：状态徽标
            badge = QLabel()
            badge.setObjectName("readyBadge" if info.status.is_ready else "notReadyBadge")
            badge.setText(tr("common.ready") if info.status.is_ready else tr("common.not_ready"))
            grid.addWidget(badge, row, 1)

            # 列 2：位置 / 下载信息
            grid.addWidget(self._build_location_cell(info), row, 2)

            # 列 3：操作按钮
            grid.addWidget(self._build_actions_cell(info), row, 3)

        self._rows_host = host
        self._rows_layout.addWidget(host)

    def _build_location_cell(self, info: ResourceInfo) -> QWidget:
        """位置列：下载进度 / 就绪路径 / 需下载体积 / 内置提示。"""
        cell = QWidget()
        layout = QVBoxLayout(cell)
        layout.setContentsMargins(0, 0, 0, 0)

        if self._downloader.is_downloading(info.key):
            progress = QLabel(tr("settings.downloading", pct="0", speed="…"))
            progress.setStyleSheet("color: #2563EB;")
            self._row_refs[info.key] = {"progress": progress}
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

        if self._downloader.is_downloading(info.key):
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
        """语言切换后刷新本窗口内的静态文案（资源行整体重建）。"""
        self.setWindowTitle(tr("settings.title"))
        self._lang_label.setText(tr("settings.language"))
        self._lang_hint.setText(tr("settings.language_hint"))
        self._resources_title.setText(tr("settings.resources"))
        for label, key in zip(
            self._header_labels,
            ("settings.col_function", "settings.col_status", "settings.col_location", "settings.col_actions"),
        ):
            label.setText(tr(key))
        self._rebuild_resource_rows()

    # ------------------------------------------------------------------ 下载

    def _on_download_clicked(self, key: str) -> None:
        """点击下载：选择位置（默认预填标准目录）后启动后台下载。"""
        info = self._manager.get(key)
        meta = RESOURCE_META.get(key)
        if meta is None or not meta.repo_id:
            # 内置资源（如 FFmpeg）不提供下载
            return

        default_target = get_models_root() / meta.default_subdir
        try:
            default_target.parent.mkdir(parents=True, exist_ok=True)
            dialog_dir = str(default_target.parent)
        except OSError:
            dialog_dir = str(Path.home())

        chosen = QFileDialog.getExistingDirectory(
            self, tr("settings.choose_download_dir"), dialog_dir
        )
        if not chosen:
            return

        # 用户选择的是"模型存放文件夹"，模型文件放入以其名称命名的子目录
        target = Path(chosen) / Path(meta.default_subdir).name
        if target != default_target:
            self.log_requested.emit(
                f"{info.model_name} -> {target}（自定义位置，重启后需重新指定）"
            )

        self._downloader.start_download(
            key=key,
            repo_id=meta.repo_id,
            target_dir=str(target),
            allow_patterns=meta.allow_patterns,
        )
        self.log_requested.emit(tr("settings.download_started", name=info.model_name))
        self._rebuild_resource_rows()

    def _on_cancel_clicked(self, key: str) -> None:
        """取消正在进行的下载（等待当前文件写完后清理，见 downloader 说明）。"""
        self._downloader.cancel(key)
        self._rebuild_resource_rows()

    def _on_download_progress(self, key: str, percent: float, speed: str) -> None:
        """更新对应行的下载进度文案。"""
        refs = self._row_refs.get(key)
        if refs is not None:
            refs["progress"].setText(
                tr("settings.downloading", pct=f"{percent * 100:.0f}", speed=speed)
            )

    def _on_download_finished(self, key: str, local_path: str) -> None:
        """下载完成：默认路径按检测结果刷新；自定义位置直接标记就绪。"""
        info = self._manager.get(key)
        meta = RESOURCE_META.get(key)
        default_target = (
            get_models_root() / meta.default_subdir if meta and meta.default_subdir else None
        )
        if default_target is not None and Path(local_path) == default_target:
            self._manager.refresh(key)
        else:
            self._manager.update_status(key, ResourceStatus.READY, local_path)
        self.log_requested.emit(tr("settings.download_done", name=info.model_name))
        self._rebuild_resource_rows()

    def _on_download_failed(self, key: str, error: str) -> None:
        """下载失败：弹窗提示并恢复行的初始状态。"""
        info = self._manager.get(key)
        self.log_requested.emit(tr("settings.download_failed", name=info.model_name, error=error))
        QMessageBox.warning(
            self, tr("settings.title"), tr("settings.download_failed", name=info.model_name, error=error)
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
