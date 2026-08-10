"""日志页: 作业历史、失败原因分类统计、日志目录入口。

失败原因分类沿用 CLI fail-summary 的口径。这个面板存在的意义是: 实测出现过
58% 失败率, 真正原因是中转站渠道断供而不是限流 —— 分不清这两者就会往错误的
方向改配置。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path

from PySide6.QtWidgets import (QHBoxLayout, QHeaderView, QLabel, QPushButton,
                               QTableWidget, QTableWidgetItem, QWidget)

from app.jobs import protocol
from app.runtime import logging_setup, paths
from app.services import job_service

from .. import tasks, theme, widgets
from .base import Page

# 与 src/generate.py 的 _classify_failure 分类一一对应
REASON_LABELS = {
    "channel_unavailable": "渠道不可用(中转站没货, 重试无意义, 需等恢复)",
    "rate_limited": "限流 429(等待后重试可能有效)",
    "service_unavailable": "服务不可用 503",
    "server_error": "服务端 5xx 错误",
    "timeout": "请求超时",
    "bad_response": "响应无法解析/未返回图像",
    "image_open_failed": "干净图打开失败(文件损坏或路径问题)",
    "synthesis_exception": "合成流程内部异常(需看日志排查)",
    "exhausted_retries": "三次重试后仍失败(汇总记录)",
    "other": "其它/未分类",
}


class LogsPage(Page):
    title = "日志与诊断"
    subtitle = "作业历史、失败原因分类, 以及日志文件位置。"
    nav_label = "日志"

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(palette, parent)

        self.banner = widgets.Banner(palette)
        self.content_layout.addWidget(self.banner)

        jobs_card = widgets.Card("作业历史", "")
        self.jobs_table = QTableWidget(0, 6)
        self.jobs_table.setHorizontalHeaderLabels(
            ["作业 ID", "状态", "进度", "合格", "驳回", "失败"])
        self.jobs_table.verticalHeader().setVisible(False)
        self.jobs_table.setAlternatingRowColors(True)
        self.jobs_table.setAccessibleName("作业历史表")
        header = self.jobs_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, 6):
            header.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        self.jobs_table.setMinimumHeight(180)
        jobs_card.body().addWidget(self.jobs_table)
        self.content_layout.addWidget(jobs_card)

        fail_card = widgets.Card(
            "失败原因分类",
            "来自 outputs/fail_log.jsonl, 与命令行 fail-summary 同一口径")
        self.fail_table = QTableWidget(0, 3)
        self.fail_table.setHorizontalHeaderLabels(["次数", "分类", "说明"])
        self.fail_table.verticalHeader().setVisible(False)
        self.fail_table.setAlternatingRowColors(True)
        self.fail_table.setAccessibleName("失败原因分类表")
        fail_header = self.fail_table.horizontalHeader()
        fail_header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        fail_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        fail_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.fail_table.setMinimumHeight(140)
        fail_card.body().addWidget(self.fail_table)
        self.fail_note = QLabel("")
        self.fail_note.setObjectName("Hint")
        self.fail_note.setWordWrap(True)
        fail_card.body().addWidget(self.fail_note)
        self.content_layout.addWidget(fail_card)

        files_card = widgets.Card("文件位置", "")
        self.paths_label = QLabel("")
        self.paths_label.setObjectName("Hint")
        self.paths_label.setWordWrap(True)
        self.paths_label.setTextInteractionFlags(
            self.paths_label.textInteractionFlags()
            | (self.paths_label.textInteractionFlags().__class__(1)))
        files_card.body().addWidget(self.paths_label)

        row = QHBoxLayout()
        row.addStretch(1)
        refresh = QPushButton("刷新")
        refresh.setAccessibleName("刷新日志与诊断信息")
        refresh.clicked.connect(self.refresh)
        row.addWidget(refresh)
        open_logs = QPushButton("打开日志目录")
        open_logs.setAccessibleName("在文件管理器中打开日志目录")
        open_logs.clicked.connect(lambda: self._open(self._logs_dir()))
        row.addWidget(open_logs)
        open_out = QPushButton("打开产物目录")
        open_out.setAccessibleName("在文件管理器中打开产物目录")
        open_out.clicked.connect(lambda: self._open(self._outputs_dir()))
        row.addWidget(open_out)
        files_card.body().addLayout(row)
        self.content_layout.addWidget(files_card)

        self.add_stretch()

    # ---- 刷新 ----

    def refresh(self) -> None:
        tasks.submit(self._gather, on_success=self._render,
                     on_error=lambda msg: self.banner.show_message(msg, "error"))

    def _gather(self) -> dict:
        jobs = []
        for status in job_service.recent(limit=30):
            progress, _ = job_service.load_progress(status)
            jobs.append((status, progress))

        reasons: Counter = Counter()
        total = 0
        fail_log = self._outputs_dir() / "fail_log.jsonl"
        if fail_log.exists():
            with open(fail_log, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        reasons[json.loads(line).get("reason", "other")] += 1
                        total += 1
                    except json.JSONDecodeError:
                        continue
        return {"jobs": jobs, "reasons": reasons, "total": total,
                "fail_log": fail_log}

    def _render(self, data: dict) -> None:
        jobs = data["jobs"]
        self.jobs_table.setRowCount(0)
        for status, progress in jobs:
            row = self.jobs_table.rowCount()
            self.jobs_table.insertRow(row)
            pct = (f"{progress.done}/{progress.pending}"
                   if progress.pending else "-")
            values = [status.job_id, status.state, pct,
                      str(progress.ok + progress.best_effort),
                      str(progress.rejected), str(progress.failed)]
            for column, value in enumerate(values):
                self.jobs_table.setItem(row, column, QTableWidgetItem(value))
        if not jobs:
            self.jobs_table.setRowCount(1)
            self.jobs_table.setItem(0, 0, QTableWidgetItem("还没有任何作业"))

        reasons: Counter = data["reasons"]
        self.fail_table.setRowCount(0)
        for reason, count in reasons.most_common():
            row = self.fail_table.rowCount()
            self.fail_table.insertRow(row)
            self.fail_table.setItem(row, 0, QTableWidgetItem(str(count)))
            self.fail_table.setItem(row, 1, QTableWidgetItem(reason))
            self.fail_table.setItem(
                row, 2, QTableWidgetItem(REASON_LABELS.get(reason, "")))
        if not reasons:
            self.fail_note.setText(
                f"没有失败记录(或还没跑过)。日志文件: {data['fail_log']}")
        else:
            self.fail_note.setText(
                f"共 {data['total']} 条失败记录。若以 渠道不可用 为主, "
                f"说明中转站暂时没有可用渠道, 重试无意义, 等恢复后重跑即可"
                f"(断点续跑不会重复消耗额度)。")

        log_file = logging_setup.current_log_file()
        self.paths_label.setText(
            f"日志目录: {self._logs_dir()}\n"
            f"当前日志: {log_file or '(未启用文件日志)'}\n"
            f"产物目录: {self._outputs_dir()}\n"
            f"作业目录: {self._jobs_dir()}")

    # ---- 工具 ----

    def _logs_dir(self) -> Path:
        try:
            return paths.logs_dir()
        except paths.WorkspaceNotConfigured:
            return paths.user_config_dir() / "logs"

    def _outputs_dir(self) -> Path:
        try:
            from src.config import load_config
            cfg = load_config()
            return cfg.resolve(cfg.output.get("root", "outputs"))
        except Exception:  # noqa: BLE001
            return self._logs_dir().parent / "outputs"

    def _jobs_dir(self) -> Path:
        try:
            return paths.jobs_dir()
        except paths.WorkspaceNotConfigured:
            return self._logs_dir().parent / "jobs"

    def _open(self, path: Path) -> None:
        path = Path(path)
        try:
            path.mkdir(parents=True, exist_ok=True)
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # noqa: S606 - 打开用户自己的目录
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path)])
        except OSError as e:
            self.banner.show_message(f"无法打开目录: {e}", "error")

    def on_palette_changed(self, palette: theme.Palette) -> None:
        self.banner.set_palette(palette)
