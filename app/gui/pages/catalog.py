"""缺陷库页: 体检结果与数据包安装。"""
from __future__ import annotations

from PySide6.QtWidgets import (QFileDialog, QGridLayout, QHBoxLayout, QLabel,
                               QMessageBox, QProgressBar, QPushButton, QWidget)

from app.runtime import paths
from app.services import catalog_service

from .. import tasks, theme, widgets
from .base import Page


class CatalogPage(Page):
    title = "缺陷库"
    subtitle = ("缺陷库是生成时的参考标准, 作为独立数据包分发, 不随程序安装。"
                "需求上明确不重建缺陷库, 因此这里只做体检与安装。")
    nav_label = "缺陷库"

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(palette, parent)

        self.banner = widgets.Banner(palette)
        self.content_layout.addWidget(self.banner)

        health = widgets.Card("体检", "与命令行 inspect 使用同一份判定逻辑")
        self.health_card = health
        self.status = widgets.StatusDot("检查中…", "muted", palette)
        health.body().addWidget(self.status)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.stat_total = widgets.StatCard("条目总数", "-", "accent", palette)
        self.stat_usable = widgets.StatCard("可用参考", "-", "ok", palette)
        self.stat_missing = widgets.StatCard("裁剪图缺失", "-", "error", palette)
        self.stat_oversized = widgets.StatCard("超裁块上限", "-", "warning", palette)
        for i, card in enumerate((self.stat_total, self.stat_usable,
                                  self.stat_missing, self.stat_oversized)):
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        holder = QWidget()
        holder.setLayout(grid)
        health.body().addWidget(holder)

        self.detail = QLabel("")
        self.detail.setObjectName("Hint")
        self.detail.setWordWrap(True)
        health.body().addWidget(self.detail)

        row = QHBoxLayout()
        row.addStretch(1)
        refresh = QPushButton("重新体检")
        refresh.setAccessibleName("重新体检缺陷库")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        health.body().addLayout(row)
        self.content_layout.addWidget(health)

        install = widgets.Card(
            "安装数据包",
            "选择 catalog-data.zip。安装会校验每个文件的哈希, 并整体替换现有缺陷库。")
        self.install_card = install
        self.pack_info = QLabel("尚未选择数据包")
        self.pack_info.setObjectName("Hint")
        self.pack_info.setWordWrap(True)
        install.body().addWidget(self.pack_info)

        self.install_progress = QProgressBar()
        self.install_progress.setRange(0, 100)
        self.install_progress.setVisible(False)
        self.install_progress.setAccessibleName("安装进度")
        install.body().addWidget(self.install_progress)

        actions = QHBoxLayout()
        actions.addStretch(1)
        self.choose_button = QPushButton("选择数据包…")
        self.choose_button.setAccessibleName("选择缺陷库数据包")
        self.choose_button.clicked.connect(self._choose)
        actions.addWidget(self.choose_button)
        self.install_button = widgets.primary_button("安装")
        self.install_button.setEnabled(False)
        self.install_button.clicked.connect(self._install)
        actions.addWidget(self.install_button)
        install.body().addLayout(actions)
        self.content_layout.addWidget(install)

        self.add_stretch()
        self._pack = None

    # ---- 体检 ----

    def refresh(self) -> None:
        self.status.set_state("检查中…", "muted")
        tasks.submit(catalog_service.inspect, on_success=self._on_report,
                     on_error=lambda msg: self.status.set_state(
                         f"检查失败: {msg}", "error"))

    def _on_report(self, report) -> None:
        headline = {"ok": "正常", "warning": "可用但有提示",
                    "error": "不可用"}.get(report.status, "未知")
        tone = {"ok": "ok", "warning": "warning", "error": "error"}.get(
            report.status, "muted")
        self.status.set_state(headline, tone)

        self.stat_total.set_value(report.total if report.exists else "-")
        self.stat_usable.set_value(report.usable if report.exists else "-")
        self.stat_missing.set_value(len(report.missing_crops))
        self.stat_oversized.set_value(len(report.oversized))

        lines = []
        if not report.exists:
            lines.append(f"未找到缺陷库: {report.catalog_path}")
            lines.append("请在下方安装 catalog-data.zip。")
        else:
            lines.append(f"位置: {report.catalog_path}")
            if report.by_type:
                lines.append("按类型: " + ", ".join(
                    f"{k} {v}" for k, v in report.by_type.items()))
            if report.by_severity:
                lines.append("按严重度: " + ", ".join(
                    f"{k}={v}" for k, v in report.by_severity.items()))
            if report.by_class:
                lines.append(f"覆盖 {len(report.by_class)} 个类别")
            if report.total != report.usable:
                lines.append(
                    f"总数 {report.total} 与可用 {report.usable} 的差额来自 "
                    f"severity 门槛过滤(过弱的缺陷不适合当复刻标准)。")
            if report.legacy_format:
                lines.append(f"有 {report.legacy_format} 条为旧格式, 缺细化字段。")
            if report.missing_source_size:
                lines.append(
                    f"有 {len(report.missing_source_size)} 条缺 source_size, "
                    f"在没有原始缺陷图的机器上裁块尺寸会偏大。")
        self.detail.setText("\n".join(lines))

    # ---- 安装 ----

    def _choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "选择缺陷库数据包", str(paths.install_root()),
            "数据包 (*.zip);;所有文件 (*.*)")
        if not path:
            return
        try:
            self._pack = catalog_service.read_pack_info(path)
        except catalog_service.PackError as e:
            self._pack = None
            self.install_button.setEnabled(False)
            self.pack_info.setText(str(e))
            self.banner.show_message(str(e), "error")
            return
        self.banner.clear()
        self.pack_info.setText(f"{self._pack.path.name}\n{self._pack.summary}")
        self.install_button.setEnabled(True)

    def _install(self) -> None:
        if self._pack is None:
            return
        confirm = QMessageBox.question(
            self, "确认安装",
            f"将用这个数据包替换当前缺陷库:\n\n{self._pack.summary}\n\n"
            f"目标目录: {paths.catalog_dir()}\n\n现有缺陷库会被覆盖, 是否继续?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            return

        self.install_button.setEnabled(False)
        self.choose_button.setEnabled(False)
        self.install_progress.setValue(0)
        self.install_progress.setVisible(True)
        self.banner.show_message("正在安装, 请勿关闭程序…", "info")

        def on_progress(done: int, total: int, text: str) -> None:
            if total:
                self.install_progress.setValue(int(done * 100 / total))
            self.install_progress.setFormat(f"{text} {done}/{total}")

        def on_ok(report) -> None:
            self.banner.show_message("缺陷库安装完成", "ok")
            self._on_report(report)

        def on_err(msg: str) -> None:
            self.banner.show_message(f"安装失败: {msg}", "error")

        def on_done() -> None:
            self.install_progress.setVisible(False)
            self.install_button.setEnabled(True)
            self.choose_button.setEnabled(True)

        tasks.submit(catalog_service.install_pack, self._pack.path,
                     wants_progress=True, on_progress=on_progress,
                     on_success=on_ok, on_error=on_err, on_done=on_done)

    def on_palette_changed(self, palette: theme.Palette) -> None:
        self.status.set_palette(palette)
        for card in (self.stat_total, self.stat_usable, self.stat_missing,
                     self.stat_oversized):
            card.set_palette(palette)
        self.banner.set_palette(palette)
