"""概览页。

设计意图: README 里"开跑前的三步自检"(selftest / inspect / test-api)是三条要背的
命令, 而且数据集路径对不对要等到跑起来才知道。这一页把这四件事变成一屏可见的
状态卡片, 每张卡片给出结论和"下一步做什么"的按钮。
"""
from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QLabel, QWidget

from app.runtime import paths, secrets
from app.services import api_service, catalog_service, dataset_service

from .. import tasks, theme, widgets
from .base import Page

# 工作区所在磁盘低于这个可用空间就告警。产物可达数十 GB, 20 GB 是个保守下限。
LOW_DISK_GB = 20


class _StatusCard(widgets.Card):
    """状态卡: 状态点 + 结论 + 一个操作按钮。"""

    def __init__(self, title: str, palette: theme.Palette,
                 action_text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(title, parent=parent)
        self.status = widgets.StatusDot("检查中…", "muted", palette)
        self.body().addWidget(self.status)

        self.detail = QLabel("")
        self.detail.setObjectName("Hint")
        self.detail.setWordWrap(True)
        # 卡片里要显示完整路径, 不限制最小宽度的话两列宽度会不相等
        self.detail.setMinimumWidth(1)
        self.body().addWidget(self.detail)

        self.action = widgets.link_button(action_text) if action_text else None
        if self.action is not None:
            self.body().addWidget(self.action)

    def set_state(self, tone: str, headline: str, detail: str = "") -> None:
        self.status.set_state(headline, tone)
        self.detail.setText(detail)
        self.detail.setVisible(bool(detail))


class OverviewPage(Page):
    title = "概览"
    subtitle = "开跑前先把这四项确认为绿色。除连通性测试外都不消耗额度。"
    nav_label = "概览"

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(palette, parent)

        grid = QGridLayout()
        grid.setSpacing(16)

        self.card_workspace = _StatusCard("工作区", palette, "前往设置")
        self.card_catalog = _StatusCard("缺陷库", palette, "管理缺陷库")
        self.card_dataset = _StatusCard("数据集", palette, "选择数据目录")
        self.card_api = _StatusCard("接口连通性", palette, "测试连接(消耗少量额度)")

        grid.addWidget(self.card_workspace, 0, 0)
        grid.addWidget(self.card_catalog, 0, 1)
        grid.addWidget(self.card_dataset, 1, 0)
        grid.addWidget(self.card_api, 1, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        holder = QWidget()
        holder.setLayout(grid)
        self.content_layout.addWidget(holder)

        self.card_workspace.action.clicked.connect(
            lambda: self.request_navigate.emit("settings"))
        self.card_catalog.action.clicked.connect(
            lambda: self.request_navigate.emit("catalog"))
        self.card_dataset.action.clicked.connect(
            lambda: self.request_navigate.emit("settings"))
        self.card_api.action.clicked.connect(self._test_api)

        self.add_stretch()
        self._api_running = False

    # ---- 刷新 ----

    def refresh(self) -> None:
        self._refresh_workspace()
        self._refresh_catalog()
        self._refresh_dataset()
        self._refresh_api_hint()

    def _refresh_workspace(self) -> None:
        workspace = paths.find_workspace()
        if workspace is None:
            self.card_workspace.set_state(
                "error", "尚未设置", "请先选择一个可写目录用于存放配置、缺陷库与产物")
            return
        free_gb = paths.disk_free_bytes(workspace) / 1024 ** 3
        writable = paths.is_writable(workspace)
        if not writable:
            self.card_workspace.set_state(
                "error", "不可写", f"{workspace}\n请换一个有写入权限的目录")
            return
        tone = "warning" if free_gb < LOW_DISK_GB else "ok"
        note = (f"所在磁盘可用 {free_gb:.0f} GB。"
                + ("产物可达数十 GB, 建议换到更大的磁盘。"
                   if free_gb < LOW_DISK_GB else ""))
        self.card_workspace.set_state(tone, "可用", f"{workspace}\n{note}")

    def _refresh_catalog(self) -> None:
        self.card_catalog.set_state("muted", "检查中…")
        tasks.submit(catalog_service.inspect,
                     on_success=self._on_catalog,
                     on_error=lambda msg: self.card_catalog.set_state(
                         "error", "检查失败", msg))

    def _on_catalog(self, report) -> None:
        tone = {"ok": "ok", "warning": "warning", "error": "error"}.get(
            report.status, "muted")
        headline = {"ok": "正常", "warning": "可用但有提示",
                    "error": "不可用"}.get(report.status, "未知")
        detail = report.summary
        if report.exists and report.oversized:
            detail += f"; {len(report.oversized)} 条缺陷长边超过裁块上限"
        self.card_catalog.set_state(tone, headline, detail)

    def _refresh_dataset(self) -> None:
        self.card_dataset.set_state("muted", "扫描中…")

        def scan():
            from src.config import load_config
            cfg = load_config()
            return dataset_service.scan(
                cfg.clean_root(),
                cfg.data.get("extensions", dataset_service.DEFAULT_EXTENSIONS),
                cfg=cfg)

        tasks.submit(scan, on_success=self._on_dataset,
                     on_error=lambda msg: self.card_dataset.set_state(
                         "error", "扫描失败", msg))

    def _on_dataset(self, report) -> None:
        if report.ok:
            self.card_dataset.set_state("ok", "已识别", report.summary)
        else:
            hint = report.hints[0] if report.hints else "未识别到任何类别"
            self.card_dataset.set_state("error", "不可用", hint)

    def _refresh_api_hint(self) -> None:
        if self._api_running:
            return
        source = secrets.key_source()
        if source == "none":
            self.card_api.set_state(
                "error", "未设置 API key", "在设置页填写, 会加密保存在系统凭据管理器")
            return
        origin = {"env": "来自环境变量 RELAY_API_KEY",
                  "credential_manager": "来自系统凭据管理器"}.get(source, "")
        self.card_api.set_state(
            "warning", "已配置, 未测试",
            f"{origin}。点下面的按钮做一次真实调用验证(会消耗少量额度)。")

    # ---- 连通性测试 ----

    def _test_api(self) -> None:
        if self._api_running:
            return
        self._api_running = True
        self.card_api.action.setEnabled(False)
        self.card_api.set_state("muted", "测试中…", "正在各调用一次视觉与编辑模型")
        tasks.submit(api_service.test_connectivity,
                     on_success=self._on_api,
                     on_error=lambda msg: self.card_api.set_state(
                         "error", "测试失败", msg),
                     on_done=self._api_done)

    def _api_done(self) -> None:
        self._api_running = False
        self.card_api.action.setEnabled(True)

    def _on_api(self, result) -> None:
        lines = [f"{s.name}: {'通过' if s.ok else '失败'} "
                 f"({s.elapsed:.1f}s) {s.detail}".rstrip()
                 for s in result.steps]
        if result.base_url:
            lines.insert(0, f"中转站: {result.base_url}")
        self.card_api.set_state("ok" if result.ok else "error",
                                result.summary, "\n".join(lines))

    def on_palette_changed(self, palette: theme.Palette) -> None:
        for card in (self.card_workspace, self.card_catalog,
                     self.card_dataset, self.card_api):
            card.status.set_palette(palette)
