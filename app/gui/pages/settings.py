"""设置页: 工作区、数据目录、中转站与密钥、并发、主题。

密钥处理遵守 requirements.md FR-3.2/NFR-2:
  只显示掩码, 保存到系统凭据管理器, 绝不写进 config.yaml 或 settings.json。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QLineEdit, QMessageBox, QPushButton,
                               QSpinBox, QWidget)

from app.runtime import paths, secrets, settings as user_settings
from app.services import api_service, dataset_service

from .. import tasks, theme, widgets
from .base import Page

LOW_DISK_GB = 20


class SettingsPage(Page):
    title = "设置"
    subtitle = "这里的改动会立即保存。生成算法参数仍在工作区的 config.yaml 里调。"
    nav_label = "设置"

    theme_changed = Signal(str)
    workspace_changed = Signal()

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(palette, parent)

        self.banner = widgets.Banner(palette)
        self.content_layout.addWidget(self.banner)

        self._build_workspace_card()
        self._build_dataset_card()
        self._build_api_card()
        self._build_appearance_card()
        self.add_stretch()

    # ---- 工作区 ----

    def _build_workspace_card(self) -> None:
        card = widgets.Card(
            "工作区",
            "存放配置、缺陷库与生成产物。产物可达数十 GB, 请选大容量磁盘。")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.workspace_edit = QLineEdit()
        self.workspace_edit.setReadOnly(True)
        self.workspace_edit.setAccessibleName("工作区目录")
        row.addWidget(self.workspace_edit, 1)
        button = QPushButton("更改…")
        button.setAccessibleName("更改工作区目录")
        button.clicked.connect(self._choose_workspace)
        row.addWidget(button)
        card.body().addLayout(row)

        self.workspace_note = QLabel("")
        self.workspace_note.setObjectName("Hint")
        self.workspace_note.setWordWrap(True)
        card.body().addWidget(self.workspace_note)
        self.content_layout.addWidget(card)

    def _choose_workspace(self) -> None:
        current = paths.find_workspace()
        start = str(current) if current else ""
        chosen = QFileDialog.getExistingDirectory(self, "选择工作区目录", start)
        if not chosen:
            return
        try:
            paths.set_workspace(Path(chosen))
        except (OSError, PermissionError) as e:
            self.banner.show_message(f"无法使用该目录: {e}", "error")
            return
        self.banner.show_message(
            "工作区已更改。缺陷库与产物都会存放在新目录下, "
            "如需继续之前的进度请把原 outputs 目录一并复制过来。", "warning")
        self._load_workspace()
        self.workspace_changed.emit()

    def _load_workspace(self) -> None:
        workspace = paths.find_workspace()
        if workspace is None:
            self.workspace_edit.setText("")
            self.workspace_note.setText("尚未设置")
            return
        self.workspace_edit.setText(str(workspace))
        free_gb = paths.disk_free_bytes(workspace) / 1024 ** 3
        note = f"所在磁盘可用 {free_gb:.0f} GB"
        if free_gb < LOW_DISK_GB:
            note += " —— 偏小, 建议换到更大的磁盘"
        if not paths.is_writable(workspace):
            note += " —— 该目录不可写"
        self.workspace_note.setText(note)

    # ---- 数据目录 ----

    def _build_dataset_card(self) -> None:
        card = widgets.Card(
            "无缺陷样本目录",
            "直接选择目录即可, 不再要求放在程序上一级、也不限制目录名里的括号形态。")
        row = QHBoxLayout()
        row.setSpacing(10)
        self.clean_edit = QLineEdit()
        self.clean_edit.setReadOnly(True)
        self.clean_edit.setAccessibleName("无缺陷样本根目录")
        row.addWidget(self.clean_edit, 1)
        button = QPushButton("选择…")
        button.setAccessibleName("选择无缺陷样本根目录")
        button.clicked.connect(self._choose_clean_root)
        row.addWidget(button)
        card.body().addLayout(row)

        self.clean_note = QLabel("")
        self.clean_note.setObjectName("Hint")
        self.clean_note.setWordWrap(True)
        card.body().addWidget(self.clean_note)
        self.content_layout.addWidget(card)

    def _choose_clean_root(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "选择无缺陷样本根目录", self.clean_edit.text())
        if not chosen:
            return
        user_settings.update(clean_root=chosen)
        self._write_clean_root_to_config(chosen)
        self.clean_edit.setText(chosen)
        self._scan_clean_root(chosen)

    def _write_clean_root_to_config(self, path: str) -> None:
        """把数据目录写回工作区的 config.yaml。

        写配置而不是只存在 settings.json: 这样 CLI 与后台 worker 读同一份配置就
        能拿到正确路径, 不必再传参数, 也保持了"config.yaml 是唯一配置来源"的
        既有习惯。
        """
        import yaml
        try:
            cfg_path = paths.workspace_config_path()
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            data.setdefault("data", {})["clean_root"] = path
            cfg_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8")
        except Exception as e:  # noqa: BLE001 - 写配置失败要让用户知道
            self.banner.show_message(f"写入 config.yaml 失败: {e}", "error")

    def _scan_clean_root(self, path: str) -> None:
        self.clean_note.setText("扫描中…")

        def done(report) -> None:
            if report.ok:
                names = ", ".join(report.class_names[:12])
                more = " 等" if len(report.class_names) > 12 else ""
                self.clean_note.setText(f"{report.summary}\n识别到: {names}{more}")
                self.banner.show_message(report.summary, "ok")
            else:
                self.clean_note.setText("\n".join(report.hints)
                                        or "未识别到任何类别")
                self.banner.show_message("数据目录不可用, 见下方提示", "error")

        tasks.submit(dataset_service.scan, path, on_success=done,
                     on_error=lambda msg: self.clean_note.setText(msg))

    # ---- 中转站与密钥 ----

    def _build_api_card(self) -> None:
        card = widgets.Card(
            "中转站与密钥",
            "key 会加密保存在 Windows 凭据管理器, 不会写入 config.yaml, "
            "也不会打包进程序。")
        form = QGridLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        form.addWidget(QLabel("接口地址"), 0, 0)
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("留空则沿用 config.yaml 里的值")
        self.base_url.setAccessibleName("中转站接口地址")
        self.base_url.editingFinished.connect(self._save_base_url)
        form.addWidget(self.base_url, 0, 1, 1, 2)

        form.addWidget(QLabel("API key"), 1, 0)
        self.api_key = QLineEdit()
        self.api_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key.setPlaceholderText("sk-...")
        self.api_key.setAccessibleName("API key")
        form.addWidget(self.api_key, 1, 1)
        save_key = QPushButton("保存")
        save_key.setAccessibleName("保存 API key")
        save_key.clicked.connect(self._save_key)
        form.addWidget(save_key, 1, 2)

        self.key_note = QLabel("")
        self.key_note.setObjectName("Hint")
        self.key_note.setWordWrap(True)
        form.addWidget(self.key_note, 2, 1, 1, 2)

        form.addWidget(QLabel("并发数"), 3, 0)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setFixedWidth(120)
        self.workers.setAccessibleName("默认并发线程数")
        self.workers.valueChanged.connect(
            lambda v: user_settings.update(max_workers=int(v)))
        form.addWidget(self.workers, 3, 1)
        form.setColumnStretch(1, 1)
        card.body().addLayout(form)

        row = QHBoxLayout()
        row.addStretch(1)
        clear = QPushButton("清除已保存的 key")
        clear.setAccessibleName("清除已保存的 API key")
        clear.clicked.connect(self._clear_key)
        row.addWidget(clear)
        self.test_button = QPushButton("测试连接(消耗少量额度)")
        self.test_button.setAccessibleName("测试中转站连通性")
        self.test_button.clicked.connect(self._test)
        row.addWidget(self.test_button)
        card.body().addLayout(row)

        self.test_note = QLabel("")
        self.test_note.setObjectName("Hint")
        self.test_note.setWordWrap(True)
        card.body().addWidget(self.test_note)
        self.content_layout.addWidget(card)

    def _save_base_url(self) -> None:
        value = self.base_url.text().strip()
        user_settings.update(base_url=value)
        if not value:
            return
        import yaml
        try:
            cfg_path = paths.workspace_config_path()
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            data.setdefault("api", {})["base_url"] = value
            cfg_path.write_text(
                yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
                encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            self.banner.show_message(f"写入 config.yaml 失败: {e}", "error")

    def _save_key(self) -> None:
        value = self.api_key.text().strip()
        if not value:
            self.banner.show_message("请先输入 key", "warning")
            return
        try:
            secrets.set_api_key(value)
        except Exception as e:  # noqa: BLE001
            self.banner.show_message(f"保存失败: {e}", "error")
            return
        self.api_key.clear()
        self.banner.show_message("key 已加密保存到系统凭据管理器", "ok")
        self._load_key_note()

    def _clear_key(self) -> None:
        confirm = QMessageBox.question(
            self, "确认清除", "确定要清除已保存的 API key 吗?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return
        secrets.clear_api_key()
        self.banner.show_message("已清除", "info")
        self._load_key_note()

    def _load_key_note(self) -> None:
        source = secrets.key_source()
        if source == "none":
            self.key_note.setText("未设置。填写后点保存。")
            return
        masked = secrets.mask(secrets.get_api_key())
        origin = {"env": "来自环境变量 RELAY_API_KEY(优先级高于已保存的 key)",
                  "credential_manager": "来自系统凭据管理器"}.get(source, "")
        self.key_note.setText(f"当前: {masked} · {origin}")

    def _test(self) -> None:
        self.test_button.setEnabled(False)
        self.test_note.setText("测试中: 正在各调用一次视觉模型与图像编辑模型…")

        def done(result) -> None:
            lines = [f"{s.name}: {'通过' if s.ok else '失败'} ({s.elapsed:.1f}s)"
                     f" {s.detail}".rstrip() for s in result.steps]
            if result.base_url:
                lines.insert(0, f"中转站: {result.base_url}")
            self.test_note.setText("\n".join(lines))
            self.banner.show_message(result.summary,
                                     "ok" if result.ok else "error")

        tasks.submit(api_service.test_connectivity, on_success=done,
                     on_error=lambda msg: self.test_note.setText(msg),
                     on_done=lambda: self.test_button.setEnabled(True))

    # ---- 外观 ----

    def _build_appearance_card(self) -> None:
        card = widgets.Card("外观", "")
        row = QHBoxLayout()
        row.setSpacing(12)
        row.addWidget(QLabel("主题"))
        self.theme_box = QComboBox()
        self.theme_box.addItem("深色", "dark")
        self.theme_box.addItem("浅色", "light")
        self.theme_box.setAccessibleName("界面主题")
        self.theme_box.currentIndexChanged.connect(self._on_theme)
        row.addWidget(self.theme_box)
        row.addStretch(1)
        card.body().addLayout(row)
        self.content_layout.addWidget(card)

    def _on_theme(self) -> None:
        name = self.theme_box.currentData()
        user_settings.update(theme=name)
        self.theme_changed.emit(name)

    # ---- 刷新 ----

    def refresh(self) -> None:
        prefs = user_settings.load()
        self._load_workspace()
        self._load_key_note()

        self.workers.blockSignals(True)
        self.workers.setValue(prefs.max_workers or 3)
        self.workers.blockSignals(False)

        self.theme_box.blockSignals(True)
        index = self.theme_box.findData(prefs.theme or "dark")
        self.theme_box.setCurrentIndex(max(0, index))
        self.theme_box.blockSignals(False)

        try:
            from src.config import load_config
            cfg = load_config()
            self.clean_edit.setText(str(cfg.clean_root()))
            self.base_url.blockSignals(True)
            self.base_url.setText(str(cfg.api.get("base_url", "")))
            self.base_url.blockSignals(False)
            self._scan_clean_root(str(cfg.clean_root()))
        except Exception as e:  # noqa: BLE001
            self.banner.show_message(f"读取配置失败: {e}", "error")

    def on_palette_changed(self, palette: theme.Palette) -> None:
        self.banner.set_palette(palette)
