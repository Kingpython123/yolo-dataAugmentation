"""生成页: 配置并启动作业, 运行中切换为进度视图。

两个要点:
  1. 启动前先做 preflight, 把"缺陷库没装""类别没干净图""key 没填"这类问题在
     点按钮之前就说清楚, 而不是等跑起来才报错。
  2. 界面重新打开时自动附着仍在运行的作业 —— 生成跑在分离进程里, 关掉界面
     任务并不会停。
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QCheckBox, QGridLayout, QHBoxLayout, QLabel,
                               QPushButton, QSpinBox, QWidget)

from app.jobs import protocol, store
from app.jobs.spec import JobStatus
from app.runtime import settings
from app.services import dataset_service, job_service

from .. import tasks, theme, widgets
from .base import Page

# 事件轮询间隔。worker 每完成一张才发几条事件, 1 秒足够及时又不浪费。
POLL_MS = 1000

MAX_COUNT = 100000


class GeneratePage(Page):
    title = "生成"
    subtitle = "选择负责的类别与目标数量。任务在后台独立进程执行, 关闭本窗口不会中断。"
    nav_label = "生成"

    def __init__(self, palette: theme.Palette,
                 parent: QWidget | None = None) -> None:
        super().__init__(palette, parent)

        self._status: JobStatus | None = None
        self._progress = protocol.JobProgress()
        self._offset = 0

        self.banner = widgets.Banner(palette)
        self.content_layout.addWidget(self.banner)

        self._build_setup_card()
        self._build_progress_card()

        self.add_stretch()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)

    # ---- 构建 ----

    def _build_setup_card(self) -> None:
        card = widgets.Card("任务配置", "只勾选自己负责的类别, 避免与他人重复消耗额度")
        self.setup_card = card

        self.table = widgets.ClassTable()
        self.table.setMinimumHeight(220)
        self.table.selection_changed.connect(self._on_selection)
        card.body().addWidget(self.table)

        row = QHBoxLayout()
        row.setSpacing(10)
        select_all = widgets.link_button("全选")
        select_none = widgets.link_button("全不选")
        select_all.clicked.connect(lambda: self.table.set_all(True))
        select_none.clicked.connect(lambda: self.table.set_all(False))
        row.addWidget(select_all)
        row.addWidget(select_none)
        row.addStretch(1)
        card.body().addLayout(row)

        card.body().addWidget(widgets.divider())

        form = QGridLayout()
        form.setHorizontalSpacing(14)
        form.setVerticalSpacing(10)

        form.addWidget(QLabel("每类生成数量"), 0, 0)
        self.count = QSpinBox()
        self.count.setRange(1, MAX_COUNT)
        self.count.setValue(700)
        self.count.setFixedWidth(120)
        self.count.setAccessibleName("每个类别生成的样本数量")
        self.count.valueChanged.connect(self._on_selection)
        form.addWidget(self.count, 0, 1)
        hint = QLabel("达到可用参考条数才能覆盖全部参考缺陷")
        hint.setObjectName("Hint")
        form.addWidget(hint, 0, 2)

        form.addWidget(QLabel("并发数"), 1, 0)
        self.workers = QSpinBox()
        self.workers.setRange(1, 16)
        self.workers.setValue(settings.load().max_workers or 3)
        self.workers.setFixedWidth(120)
        self.workers.setAccessibleName("并发线程数")
        form.addWidget(self.workers, 1, 1)
        hint2 = QLabel("调高更快, 但中转站可能限流")
        hint2.setObjectName("Hint")
        form.addWidget(hint2, 1, 2)

        self.force = QCheckBox("强制重新生成(忽略断点续跑)")
        self.force.setAccessibleName("强制重新生成")
        self.force.stateChanged.connect(self._on_selection)
        form.addWidget(self.force, 2, 0, 1, 3)
        form.setColumnStretch(2, 1)
        card.body().addLayout(form)

        actions = QHBoxLayout()
        actions.setSpacing(10)
        self.plan_label = QLabel("")
        self.plan_label.setObjectName("Hint")
        self.plan_label.setWordWrap(True)
        actions.addWidget(self.plan_label, 1)

        self.check_button = QPushButton("检查")
        self.check_button.setAccessibleName("检查是否可以开始")
        self.check_button.clicked.connect(self._preflight)
        actions.addWidget(self.check_button)

        self.start_button = widgets.primary_button("开始生成")
        self.start_button.clicked.connect(self._start)
        actions.addWidget(self.start_button)
        card.body().addLayout(actions)

        self.content_layout.addWidget(card)

    def _build_progress_card(self) -> None:
        card = widgets.Card("运行中", "")
        self.progress_card = card
        card.setVisible(False)

        self.progress_bar = widgets.LabeledProgress()
        card.body().addWidget(self.progress_bar)

        grid = QGridLayout()
        grid.setSpacing(12)
        self.stat_ok = widgets.StatCard("合格", "0", "ok", self.palette_)
        self.stat_rejected = widgets.StatCard("驳回", "0", "warning", self.palette_)
        self.stat_failed = widgets.StatCard("失败", "0", "error", self.palette_)
        self.stat_skipped = widgets.StatCard("已跳过", "0", "muted", self.palette_)
        for i, w in enumerate((self.stat_ok, self.stat_rejected,
                               self.stat_failed, self.stat_skipped)):
            grid.addWidget(w, 0, i)
            grid.setColumnStretch(i, 1)
        holder = QWidget()
        holder.setLayout(grid)
        card.body().addWidget(holder)

        self.log = widgets.LogView(self.palette_)
        self.log.setMinimumHeight(200)
        card.body().addWidget(self.log)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.job_label = QLabel("")
        self.job_label.setObjectName("Hint")
        row.addWidget(self.job_label, 1)
        self.stop_button = QPushButton("停止")
        self.stop_button.setObjectName("Danger")
        self.stop_button.setAccessibleName("停止生成(在途任务会先落盘)")
        self.stop_button.clicked.connect(self._stop)
        row.addWidget(self.stop_button)
        card.body().addLayout(row)

        self.content_layout.addWidget(card)

    # ---- 生命周期 ----

    def refresh(self) -> None:
        self._load_classes()
        if self._status is None:
            self._try_attach()

    def _load_classes(self) -> None:
        def scan():
            from src.config import load_config
            cfg = load_config()
            return dataset_service.scan(
                cfg.clean_root(),
                cfg.data.get("extensions", dataset_service.DEFAULT_EXTENSIONS),
                cfg=cfg)

        tasks.submit(scan, on_success=self._on_classes,
                     on_error=lambda msg: self.banner.show_message(msg, "error"))

    def _on_classes(self, report) -> None:
        prefs = settings.load()
        self.table.set_classes(report.classes, preselect=prefs.last_classes)
        if prefs.last_count:
            self.count.setValue(min(MAX_COUNT, max(1, prefs.last_count)))
        if not report.ok:
            hint = report.hints[0] if report.hints else "未识别到任何类别"
            self.banner.show_message(f"数据集不可用: {hint}", "error")
        else:
            self.banner.clear()

    def _try_attach(self) -> None:
        """附着到仍在运行的作业(界面重开后接续观察)。"""
        status = job_service.attachable()
        if status is None:
            return
        self._attach(status, note="已重新附着到后台正在运行的任务")

    def _attach(self, status: JobStatus, note: str = "") -> None:
        self._status = status
        self._progress, self._offset = job_service.load_progress(status)
        self.log.clear()
        for line in self._progress.recent_logs(limit=400):
            self.log.append_line(line.text, line.level)
        self.log.scroll_to_end()
        self.progress_card.setVisible(True)
        self.setup_card.setEnabled(False)
        self.stop_button.setEnabled(True)
        self.job_label.setText(f"作业 {status.job_id}")
        if note:
            self.banner.show_message(note, "info")
        self._render()
        self._timer.start()

    # ---- 提交前检查 ----

    def _on_selection(self) -> None:
        selected = self.table.selected_classes()
        self.start_button.setEnabled(bool(selected) and self._status is None)
        if not selected:
            self.plan_label.setText("请至少勾选一个类别")
        else:
            self.plan_label.setText(
                f"已选 {len(selected)} 个类别, 每类 {self.count.value()} 张, "
                f"共 {len(selected) * self.count.value()} 张")

    def _preflight(self, then_start: bool = False) -> None:
        selected = self.table.selected_classes()
        if not selected:
            self.banner.show_message("请至少勾选一个类别", "warning")
            return
        self.check_button.setEnabled(False)
        self.start_button.setEnabled(False)

        def done(result) -> None:
            self.check_button.setEnabled(True)
            self.start_button.setEnabled(self._status is None)
            tone = "ok" if result.ok else "error"
            text = result.message
            if result.warnings:
                text += "。" + "；".join(result.warnings)
                tone = "warning" if result.ok else tone
            self.banner.show_message(text, tone)
            if result.ok:
                self.plan_label.setText(result.message)
            if then_start and result.ok and result.pending > 0:
                self._submit(selected)

        tasks.submit(job_service.preflight, selected, self.count.value(),
                     force=self.force.isChecked(),
                     on_success=done,
                     on_error=lambda msg: (
                         self.banner.show_message(msg, "error"),
                         self.check_button.setEnabled(True),
                         self.start_button.setEnabled(True)))

    def _start(self) -> None:
        self._preflight(then_start=True)

    def _submit(self, selected: list) -> None:
        def started(status) -> None:
            self._attach(status, note=f"已启动后台任务 {status.job_id}")

        tasks.submit(job_service.submit, selected, self.count.value(),
                     force=self.force.isChecked(),
                     max_workers=self.workers.value(),
                     on_success=started,
                     on_error=lambda msg: self.banner.show_message(msg, "error"))

    # ---- 运行中 ----

    def _stop(self) -> None:
        if self._status is None:
            return
        self.stop_button.setEnabled(False)
        job_service.cancel(self._status)
        self.banner.show_message(
            "已请求停止。正在处理的样本会先完整落盘, 请稍候 —— "
            "这样可以保证标注文件不被写坏。", "info")

    def _poll(self) -> None:
        if self._status is None or self._status.job_dir is None:
            self._timer.stop()
            return
        events, self._offset = store.read_events(self._status.job_dir,
                                                self._offset)
        for event in events:
            self._progress.apply(event)
            if event.get("t") == "log":
                self.log.append_line(str(event.get("msg", "")),
                                     str(event.get("level", "info")))
        if events:
            self.log.scroll_to_end()

        status = store.read_status(self._status.job_dir)
        if status is not None:
            status = store.reap_stale(status)
            self._status = status
        self._render()

        if self._status is not None and self._status.is_terminal:
            self._finish()

    def _render(self) -> None:
        p = self._progress
        self.stat_ok.set_value(p.ok + p.best_effort)
        self.stat_rejected.set_value(p.rejected)
        self.stat_failed.set_value(p.failed)
        self.stat_skipped.set_value(p.skipped + p.cancelled)

        left = f"已完成 {p.done} / {p.pending}"
        right = ""
        if p.items_per_hour:
            right = f"约 {p.items_per_hour:.0f} 张/小时 · 预计剩余 {p.format_eta()}(估算)"
        elif not p.finished:
            right = "速率估算中"
        self.progress_bar.update_progress(p.percent, left, right)

    def _finish(self) -> None:
        self._timer.stop()
        status = self._status
        self._status = None
        self.setup_card.setEnabled(True)
        self.stop_button.setEnabled(False)
        self._on_selection()

        if status is None:
            return
        mapping = {
            "completed": ("ok", "任务已完成"),
            "cancelled": ("info", "任务已停止。重新运行同样的配置会自动接续未完成的部分。"),
            "failed": ("error", f"任务失败: {status.error or '原因见日志'}"),
        }
        tone, text = mapping.get(status.state, ("info", f"任务结束({status.state})"))
        stats = status.stats or {}
        if stats:
            text += (f" 合格 {stats.get('ok', 0)} / 驳回 {stats.get('rejected', 0)}"
                     f" / 失败 {stats.get('failed', 0)}")
        self.banner.show_message(text, tone)
        self.job_label.setText(f"作业 {status.job_id} · {status.state}")
        self._load_classes()

    def on_palette_changed(self, palette: theme.Palette) -> None:
        for card in (self.stat_ok, self.stat_rejected, self.stat_failed,
                     self.stat_skipped):
            card.set_palette(palette)
        self.log.set_palette(palette)
        self.banner.set_palette(palette)

    def closing(self) -> bool:
        """主窗口关闭前询问。返回 True 表示可以关闭。"""
        return True

    def has_running_job(self) -> bool:
        return self._status is not None and not self._status.is_terminal
