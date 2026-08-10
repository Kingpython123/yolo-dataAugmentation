"""一次性迁移: 给 run.py 增加 worker / job-start / job-list / job-cancel 子命令。

设计文档原本写的是 `--worker` 全局开关, 这里改成 `worker` 子命令: 现有 CLI 已经
是 11 个子命令的结构, 且 argparse 的 required 子命令与全局开关混用会让
"没给子命令"的报错变得难以理解。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "run.py"

NEW_COMMANDS = '''

# --------------------------- 作业(供图形界面与脚本使用) ---------------------------

def cmd_worker(args):
    """执行一个作业目录里描述的任务。由界面以分离进程方式调用, 也可手工调用排障。"""
    from app.worker import run_job
    return run_job(args.job)


def cmd_job_start(args):
    """提交一个后台生成作业并立即返回。关闭终端不会中断它。"""
    from app.services import job_service
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    check = job_service.preflight(classes, args.count, force=args.force)
    for w in check.warnings:
        print(f"[warn] {w}")
    if not check.ok:
        for b in check.blockers:
            print(f"[error] {b}")
        return 2
    print(f"[plan] {check.message}")
    if check.pending == 0 and not args.force:
        return 0
    status = job_service.submit(classes, args.count, force=args.force,
                               max_workers=args.max_workers)
    print(f"[started] 作业 {status.job_id} 已在后台运行 (pid={status.pid})")
    print(f"          作业目录: {status.job_dir}")
    print(f"          查看进度: run.py job-list")
    print(f"          停止任务: run.py job-cancel --job-id {status.job_id}")
    return 0


def cmd_job_list(args):
    """列出最近的作业及其进度。"""
    from app.jobs.protocol import format_clock
    from app.services import job_service
    jobs = job_service.recent(limit=args.limit)
    if not jobs:
        print("还没有任何作业")
        return 0
    print(f"{'作业ID':22} {'状态':10} {'进度':>12}  {'合格/驳回/失败':>16}  最后活动")
    for status in jobs:
        progress, _ = job_service.load_progress(status)
        pct = f"{progress.done}/{progress.pending}" if progress.pending else "-"
        counts = (f"{progress.ok}/{progress.rejected}/{progress.failed}")
        print(f"{status.job_id:22} {status.state:10} {pct:>12}  {counts:>16}  "
              f"{format_clock(progress.last_ts)}")
    return 0


def cmd_job_cancel(args):
    """请求停止一个作业(优雅停止: 在途任务会跑完并正常落盘)。"""
    from app.jobs import store
    from app.services import job_service
    target = None
    for status in job_service.recent(limit=200):
        if status.job_id == args.job_id:
            target = status
            break
    if target is None:
        print(f"找不到作业: {args.job_id}")
        return 2
    if target.is_terminal:
        print(f"作业已结束({target.state}), 无需取消")
        return 0
    store.request_cancel(target.job_dir)
    print(f"已请求停止 {target.job_id}。在途任务会先落盘, 请稍候。")
    return 0
'''

SUBPARSERS = '''
    p = sub.add_parser("worker",
                       help="执行一个作业目录(由图形界面调用, 一般不需手工执行)")
    p.add_argument("--job", required=True, help="作业目录路径")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("job-start",
                       help="提交后台生成作业并立即返回(关终端不中断)")
    p.add_argument("--classes", required=True, help="目标类别, 逗号分隔")
    p.add_argument("--count", type=int, required=True, help="每个类别各生成多少张")
    p.add_argument("--force", action="store_true", help="忽略断点续跑")
    p.add_argument("--max-workers", type=int, default=None, help="并发线程数")
    p.set_defaults(func=cmd_job_start)

    p = sub.add_parser("job-list", help="列出最近的后台作业及进度")
    p.add_argument("--limit", type=int, default=20, help="最多列出几条")
    p.set_defaults(func=cmd_job_list)

    p = sub.add_parser("job-cancel", help="请求停止某个后台作业(优雅停止)")
    p.add_argument("--job-id", required=True, help="作业 ID, 见 job-list")
    p.set_defaults(func=cmd_job_cancel)

'''

EDITS: list[tuple[str, str, str]] = [
    (
        "追加作业相关命令实现",
        '\ndef main():\n'
        '    ap = argparse.ArgumentParser(description="工业瓶子缺陷合成流水线")\n',

        NEW_COMMANDS
        + '\ndef main():\n'
        '    ap = argparse.ArgumentParser(description="工业瓶子缺陷合成流水线")\n',
    ),
    (
        "注册作业子命令",
        '    args = ap.parse_args()\n',
        SUBPARSERS + '    args = ap.parse_args()\n',
    ),
    (
        "main(): 用子命令返回值作为退出码",
        '    bootstrap.install(workspace=getattr(args, "workspace", None))\n'
        '    args.func(args)\n',

        '    bootstrap.install(workspace=getattr(args, "workspace", None))\n'
        '    # 子命令可以返回退出码; 返回 None 视为成功, 保持原有行为\n'
        '    return args.func(args) or 0\n',
    ),
    (
        '__main__: 传递退出码',
        'if __name__ == "__main__":\n'
        '    main()\n',

        'if __name__ == "__main__":\n'
        '    raise SystemExit(main())\n',
    ),
]


def main() -> int:
    with TARGET.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    problems: list[str] = []
    for label, old, _new in EDITS:
        n = text.count(old)
        if n != 1:
            problems.append(f"  [{label}] 命中 {n} 次(期望 1)")
    if problems:
        print("以下片段未精确命中, 未做任何修改:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    for label, old, new in EDITS:
        text = text.replace(old, new, 1)
        print(f"  ok  {label}")

    if uses_crlf:
        text = text.replace("\n", "\r\n")
    with TARGET.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print(f"\n已写回 {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
