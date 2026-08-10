"""主窗口: 侧边导航 + 页面栈, 以及首次启动的工作区引导。"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QButtonGroup, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QMainWindow,
                               QMessageBox, QPushButton, QStackedWidget,
                               QVBoxLayout, QWidget)

from app.runtime import paths, settings
from app.version import APP_DISPLAY_NAME, __version__

from . import theme, widgets
from .pages.catalog import CatalogPage
from .pages.export import ExportPage
from .pages.generate import GeneratePage
from .pages.logs import LogsPage
from .pages.overview import OverviewPage
from .pages.settings import SettingsPage

WINDOW_MIN = (1120, 720)
WINDOW_DEFAULT = (1240, 820)

# 侧边导航的顺序即用户的自然流程: 先看状态 -> 装库 -> 配路径与密钥 -> 生成 -> 导出
PAGE_ORDER = (
    ("overview", OverviewPage),
    ("catalog", CatalogPage),
    ("settings", SettingsPage),
    ("generate", GeneratePage),
    ("export", ExportPage),
    ("logs", LogsPage),
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        prefs = settings.load()
        self.palette_ = theme.palette_for(prefs.theme)

        self.setWindowTitle(f"{APP_DISPLAY_NAME} {__version__}")
        self.setMinimumSize(*WINDOW_MIN)
        self.resize(*WINDOW_DEFAULT)

        central = QWidget()
        layout = QHBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._nav_buttons: dict[str, QPushButton] = {}
        self._pages: dict[str, object] = {}
        self.stack = QStackedWidget()

        layout.addWidget(self._build_sidebar())
        layout.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._build_pages()
        self.apply_theme(self.palette_.name)
        self._install_shortcuts()

        self.statusBar().showMessage(self._workspace_hint())
        self.navigate("overview")

    # ---- 侧边导航 ----

    def _build_sidebar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("Sidebar")
        bar.setFixedWidth(theme.SIDEBAR_WIDTH)

        layout = QVBoxLayout(bar)
        layout.setContentsMargins(14, 20, 14, 16)
        layout.setSpacing(6)

        name = QLabel(APP_DISPLAY_NAME)
        name.setObjectName("BrandName")
        name.setWordWrap(True)
        layout.addWidget(name)
        version = QLabel(f"版本 {__version__}")
        version.setObjectName("BrandVersion")
        layout.addWidget(version)
        layout.addSpacing(14)

        self._nav_group = QButtonGroup(self)
        self._nav_group.setExclusive(True)
        self._nav_container = QVBoxLayout()
        self._nav_container.setSpacing(4)
        layout.addLayout(self._nav_container)

        layout.addStretch(1)
        self.workspace_label = QLabel("")
        self.workspace_label.setObjectName("Hint")
        self.workspace_label.setWordWrap(True)
        layout.addWidget(self.workspace_label)
        return bar

    def _build_pages(self) -> None:
        for index, (key, page_class) in enumerate(PAGE_ORDER):
            page = page_class(self.palette_)
            self._pages[key] = page
            self.stack.addWidget(page)

            button = QPushButton(page.nav_label or key)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setAccessibleName(f"切换到{page.nav_label}页")
            button.clicked.connect(lambda _=False, k=key: self.navigate(k))
            self._nav_group.addButton(button, index)
            self._nav_container.addWidget(button)
            self._nav_buttons[key] = button

            page.request_navigate.connect(self.navigate)
            page.toast.connect(self._toast)

        settings_page = self._pages["settings"]
        settings_page.theme_changed.connect(self.apply_theme)
        settings_page.workspace_changed.connect(self._on_workspace_changed)

    def _install_shortcuts(self) -> None:
        # Ctrl+1..6 直达各页, 键盘用户不必反复 Tab
        for index, (key, _cls) in enumerate(PAGE_ORDER, start=1):
            shortcut = QShortcut(QKeySequence(f"Ctrl+{index}"), self)
            shortcut.activated.connect(lambda k=key: self.navigate(k))
        refresh = QShortcut(QKeySequence("F5"), self)
        refresh.activated.connect(self._refresh_current)

    # ---- 导航与主题 ----

    def navigate(self, key: str) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        self.stack.setCurrentWidget(page)
        button = self._nav_buttons.get(key)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        page.refresh()

    def _refresh_current(self) -> None:
        current = self.stack.currentWidget()
        refresh = getattr(current, "refresh", None)
        if callable(refresh):
            refresh()

    def apply_theme(self, name: str) -> None:
        self.palette_ = theme.palette_for(name)
        qss = theme.build_qss(self.palette_)
        # 样式表设在 QApplication 而不是窗口上: QMessageBox / QFileDialog
        # 是独立的顶层窗口, 只给主窗口上样式时它们会保持系统默认外观,
        # 在深色主题下显得很突兜。
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(qss)
        else:
            self.setStyleSheet(qss)
        for page in self._pages.values():
            page.set_palette_recursive(self.palette_)

    # ---- 工作区 ----

    def _workspace_hint(self) -> str:
        workspace = paths.find_workspace()
        if workspace is None:
            return "尚未设置工作区"
        return f"工作区: {workspace}"

    def _on_workspace_changed(self) -> None:
        self.statusBar().showMessage(self._workspace_hint())
        self.workspace_label.setText(self._workspace_hint())
        self._refresh_current()

    def ensure_workspace(self) -> bool:
        """首次启动引导。返回 False 表示用户放弃, 应退出程序。"""
        self.workspace_label.setText(self._workspace_hint())
        if paths.find_workspace() is not None:
            paths.ensure_workspace_layout()
            return True

        QMessageBox.information(
            self, "选择工作区",
            "首次运行需要选择一个工作区目录, 用来存放配置、缺陷库与生成产物。\n\n"
            "产物体积可能达到数十 GB, 请选一块空间充足的磁盘, "
            "不要放在系统盘的程序目录下。")
        while True:
            chosen = QFileDialog.getExistingDirectory(self, "选择工作区目录")
            if not chosen:
                answer = QMessageBox.question(
                    self, "未选择工作区",
                    "没有工作区就无法保存配置与产物。要退出程序吗?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer == QMessageBox.StandardButton.Yes:
                    return False
                continue
            try:
                paths.set_workspace(Path(chosen))
            except (OSError, PermissionError) as e:
                QMessageBox.warning(self, "目录不可用",
                                    f"无法使用该目录:\n{e}\n请换一个。")
                continue
            free_gb = paths.disk_free_bytes(chosen) / 1024 ** 3
            if free_gb < 20:
                answer = QMessageBox.question(
                    self, "空间偏小",
                    f"该磁盘可用空间仅 {free_gb:.0f} GB。生成产物可能达到数十 GB, "
                    f"确定继续吗?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    continue
            self._on_workspace_changed()
            return True

    # ---- 其它 ----

    def _toast(self, text: str, tone: str = "info") -> None:
        self.statusBar().showMessage(text, 6000)

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt 约定
        generate_page = self._pages.get("generate")
        running = bool(generate_page and generate_page.has_running_job())
        if running:
            answer = QMessageBox.question(
                self, "任务仍在运行",
                "生成任务在独立的后台进程里运行, 关闭本窗口不会中断它。\n\n"
                "下次打开程序会自动重新附着并继续显示进度。要关闭窗口吗?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()
