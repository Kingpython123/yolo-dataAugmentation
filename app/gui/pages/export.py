"""导出页。

替代 README 第五节的手工步骤: generated/ 与 rejected/ 都要交、annotations.jsonl
一起交、跳过约 2 GB 的 debug/。这三点在服务层是硬编码的规则, 界面只负责让用户
确认范围与体积。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import (QCheckBox, QFileDialog, QGridLayout, QHBoxLayout,
                               QLabel, QProgressBar, QPushButton, QWidget)

from app.runtime import paths
from app.services import dataset_service, export_service

from .. import tasks, theme, widgets
from .base import Page


class ExportPage(Page):
    title = "导出成果"
    subtitle = ("按交付规范打包: generated 与 rejected 都会包含, 并带上 "
                "annotations.jsonl; debug 等中间产物一律排除。")
    nav_label = "导出"

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(palette, parent)

        self.banner = widgets.Banner(palette)
        self.content_layout.addWidget(self.banner)

        scope = widgets.Card("范围", "不勾选任何类别表示导出全部")
        self.table = widgets.ClassTable()
        self.table.setMinimumHeight(200)
        self.table.selection_changed.connect(self._estimate)
        scope.body().addWidget(self.table)

        row = QHBoxLayout()
        row.setSpacing(10)
        all_button = widgets.link_button("全选")
        none_button = widgets.link_button("全不选")
        all_button.clicked.connect(lambda: self.table.set_all(True))
        none_button.clicked.connect(lambda: self.table.set_all(False))
        row.addWidget(all_button)
        row.addWidget(none_button)
        row.addStretch(1)
        self.include_masks = QCheckBox("同时包含掩膜(masks)")
        self.include_masks.setAccessibleName("导出时包含掩膜")
        self.include_masks.stateChanged.connect(self._estimate)
        row.addWidget(self.include_masks)
        scope.body().addLayout(row)
        self.content_layout.addWidget(scope)

        preview = widgets.Card("预估", "导出前先看清体积, 避免误带中间产物")
        grid = QGridLayout()
        grid.setSpacing(12)
        self.stat_files = widgets.StatCard("文件数", "-", "accent", palette)
        self.stat_size = widgets.StatCard("体积", "-", "accent", palette)
        self.stat_classes = widgets.StatCard("类别数", "-", "accent", palette)
        self.stat_lines = widgets.StatCard("标注行数", "-", "accent", palette)
        for i, card in enumerate((self.stat_files, self.stat_size,
                                 self.stat_classes, self.stat_lines)):
            grid.addWidget(card, 0, i)
            grid.setColumnStretch(i, 1)
        holder = QWidget()
        holder.setLayout(grid)
        preview.body().addWidget(holder)

        self.breakdown = QLabel("")
        self.breakdown.setObjectName("Hint")
        self.breakdown.setWordWrap(True)
        preview.body().addWidget(self.breakdown)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setVisible(False)
        self.progress.setAccessibleName("导出进度")
        preview.body().addWidget(self.progress)

        actions = QHBoxLayout()
        actions.addStretch(1)
        refresh = QPushButton("重新预估")
        refresh.setAccessibleName("重新预估导出体积")
        refresh.clicked.connect(self._estimate)
        actions.addWidget(refresh)
        self.export_button = widgets.primary_button("导出为 zip…")
        self.export_button.clicked.connect(self._export)
        actions.addWidget(self.export_button)
        preview.body().addLayout(actions)
        self.content_layout.addWidget(preview)

        self.add_stretch()
        self._estimate_obj = None

    # ---- 刷新 ----

    def refresh(self) -> None:
        def scan():
            from src.config import load_config
            cfg = load_config()
            return dataset_service.scan(
                cfg.clean_root(),
                cfg.data.get("extensions", dataset_service.DEFAULT_EXTENSIONS),
                cfg=cfg)

        def done(report) -> None:
            self.table.set_classes(report.classes)
            self._estimate()

        tasks.submit(scan, on_success=done,
                     on_error=lambda msg: self.banner.show_message(msg, "error"))

    def _estimate(self) -> None:
        selected = self.table.selected_classes()

        def done(result) -> None:
            self._estimate_obj = result
            self.stat_files.set_value(result.file_count)
            self.stat_size.set_value(f"{result.total_mb:.0f} MB")
            self.stat_classes.set_value(len(result.classes))
            self.stat_lines.set_value(result.annotations_lines)

            parts = []
            for tree, (count, size) in result.per_tree.items():
                parts.append(f"{tree}: {count} 个文件 / {size / 1024 / 1024:.0f} MB")
            if result.missing:
                parts.append("缺失: " + ", ".join(result.missing))
            parts.append("排除: " + ", ".join(export_service.EXCLUDED_DIRS))
            self.breakdown.setText("\n".join(parts))
            self.export_button.setEnabled(result.file_count > 0)

        tasks.submit(export_service.estimate, selected or None,
                     include_masks=self.include_masks.isChecked(),
                     on_success=done,
                     on_error=lambda msg: self.banner.show_message(msg, "error"))

    # ---- 导出 ----

    def _export(self) -> None:
        if self._estimate_obj is None or not self._estimate_obj.file_count:
            self.banner.show_message("没有可导出的内容", "warning")
            return

        default = str(Path.home() / export_service.default_dest_name())
        target, _ = QFileDialog.getSaveFileName(
            self, "导出成果", default, "压缩包 (*.zip)")
        if not target:
            return

        free = paths.disk_free_bytes(Path(target).parent)
        if free and free < self._estimate_obj.total_bytes:
            self.banner.show_message(
                f"目标磁盘可用 {free / 1024 ** 3:.1f} GB, "
                f"小于预估的 {self._estimate_obj.total_mb / 1024:.1f} GB, "
                f"请换个位置", "error")
            return

        selected = self.table.selected_classes()
        self.export_button.setEnabled(False)
        self.progress.setValue(0)
        self.progress.setVisible(True)
        self.banner.show_message("正在打包…", "info")

        def on_progress(done_count: int, total: int, text: str) -> None:
            if total:
                self.progress.setValue(int(done_count * 100 / total))
            self.progress.setFormat(f"{done_count}/{total}")

        def on_ok(path) -> None:
            self.banner.show_message(f"已导出: {path}", "ok")

        tasks.submit(export_service.export, target, selected or None,
                     include_masks=self.include_masks.isChecked(),
                     wants_progress=True, on_progress=on_progress,
                     on_success=on_ok,
                     on_error=lambda msg: self.banner.show_message(
                         f"导出失败: {msg}", "error"),
                     on_done=lambda: (self.progress.setVisible(False),
                                      self.export_button.setEnabled(True)))

    def on_palette_changed(self, palette: theme.Palette) -> None:
        for card in (self.stat_files, self.stat_size, self.stat_classes,
                     self.stat_lines):
            card.set_palette(palette)
        self.banner.set_palette(palette)
