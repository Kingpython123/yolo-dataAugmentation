"""作业相关用例: 提交、列出、取消、附着。

界面与 CLI 都通过这一层操作作业, 不直接碰 store/launcher, 以免两边对"提交前
该做哪些校验"产生分歧。
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.jobs import launcher, store
from app.jobs.protocol import JobProgress
from app.jobs.spec import KIND_GEN_TARGET, JobSpec, JobStatus, new_job_id
from app.runtime import paths, secrets, settings
from src import catalog_report
from src.config import load_config

from . import dataset_service


@dataclass
class PreflightResult:
    """提交前的检查结果。

    刻意把"能不能跑"的判断集中在提交前一次做完, 而不是让用户点了开始、等了
    半分钟才看到"缺陷库不存在"这类本可以立刻发现的问题。
    """

    ok: bool
    blockers: list          # 必须解决才能开始
    warnings: list          # 可以继续但需知晓
    skipped: int = 0        # 断点续跑将跳过的数量
    pending: int = 0        # 本次实际要生成的数量

    @property
    def message(self) -> str:
        if self.blockers:
            return "；".join(self.blockers)
        if self.pending == 0:
            return "所有目标样本都已生成, 无需重跑(如需重做请勾选强制重新生成)"
        return f"将跳过 {self.skipped} 项, 待生成 {self.pending} 项"


def preflight(classes: list, count: int, force: bool = False) -> PreflightResult:
    """提交前检查。不发起任何网络请求。"""
    blockers: list = []
    warnings: list = []

    if not classes:
        blockers.append("未选择任何类别")
    if count <= 0:
        blockers.append("生成数量必须大于 0")
    if not secrets.has_api_key():
        blockers.append("未设置 API key(在设置页填写)")

    try:
        cfg = load_config()
    except Exception as e:
        return PreflightResult(False, [f"配置读取失败: {e}"], warnings)

    report = catalog_report.build_report(cfg)
    if not report.exists:
        blockers.append("缺陷库未安装(在缺陷库页安装数据包)")
    elif report.missing_crops:
        blockers.append(f"缺陷库有 {len(report.missing_crops)} 条裁剪图缺失, "
                        f"请重新安装数据包")
    elif report.usable == 0:
        blockers.append("缺陷库过滤后没有可用参考, 检查 min_reference_severity")

    ds = dataset_service.scan(cfg.clean_root(), cfg.data.get(
        "extensions", dataset_service.DEFAULT_EXTENSIONS), cfg=cfg)
    if not ds.ok:
        blockers.append("数据集不可用: " + (ds.hints[0] if ds.hints else "未识别到类别"))
    else:
        missing = [c for c in classes if c not in ds.class_names]
        if missing:
            blockers.append(f"以下类别在数据集中找不到干净图: {', '.join(missing)}")

    skipped = pending = 0
    if not blockers:
        skipped, pending = _estimate_resume(cfg, classes, count, force)
        if report.usable and count < report.usable:
            warnings.append(
                f"目标数量 {count} 小于可用参考 {report.usable} 条, "
                f"本轮无法覆盖全部参考缺陷")

    return PreflightResult(not blockers, blockers, warnings, skipped, pending)


def _estimate_resume(cfg, classes: list, count: int, force: bool):
    """按与生成流程完全相同的规则算出将跳过/待生成的数量。

    复用 generate 里的任务构造逻辑而不是另写一份估算: 两份规则一定会分叉,
    而这个数字是用户决定要不要开始的依据, 不能只是个近似值。
    """
    from src import generate

    records = generate.load_catalog(cfg)
    usable = generate.usable_references(cfg, records, verbose=False) or records
    if not usable:
        return 0, 0

    clean_images = generate.scan_class_images(cfg, cfg.clean_root())
    stems: list[str] = []
    n_ref = len(usable)
    for class_name in classes:
        image_paths = clean_images.get(class_name)
        if not image_paths:
            continue
        for i in range(count):
            ref = usable[i % n_ref]
            clean = image_paths[i % len(image_paths)]
            stems.append(
                f"{generate._safe(class_name)}__{generate._safe(clean.stem)}"
                f"__t{i}__ref_{generate._safe(ref['entry_id'])}")

    if force:
        return 0, len(stems)
    done = generate._load_done(cfg.out_path("annotations"))
    pending = [s for s in stems if s not in done]
    return len(stems) - len(pending), len(pending)


def submit(classes: list, count: int, force: bool = False,
           max_workers: int | None = None) -> JobStatus:
    """提交并启动一个生成作业。"""
    prefs = settings.load()
    cfg = load_config()
    spec = JobSpec(
        job_id=new_job_id(),
        kind=KIND_GEN_TARGET,
        classes=list(classes),
        count=int(count),
        force=bool(force),
        max_workers=max_workers or prefs.max_workers or None,
        workspace=str(paths.workspace()),
        clean_root=str(cfg.clean_root()),
    )
    status = launcher.launch(spec)
    settings.update(last_classes=list(classes), last_count=int(count))
    return status


def cancel(status_or_dir) -> None:
    directory = (status_or_dir.job_dir if isinstance(status_or_dir, JobStatus)
                 else Path(status_or_dir))
    if directory is not None:
        store.request_cancel(directory)


def recent(limit: int = 20) -> list:
    """最近的作业, 顺带把"进程已死但状态还是 running"的收尾掉。"""
    return [store.reap_stale(s) for s in store.list_jobs(limit=limit)]


def attachable() -> JobStatus | None:
    """界面启动时可自动附着的作业(仍在运行的最新一个)。"""
    running = store.running_jobs()
    return running[0] if running else None


def load_progress(status: JobStatus) -> tuple[JobProgress, int]:
    """把某作业的全部历史事件折叠成进度, 返回 (进度, 事件偏移)。"""
    progress = JobProgress()
    if status.job_dir is None:
        return progress, 0
    events, offset = store.read_events(status.job_dir, 0)
    progress.apply_all(events)
    return progress, offset
