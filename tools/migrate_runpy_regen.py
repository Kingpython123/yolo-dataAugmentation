"""一次性迁移: 给 run.py 增加 regen-rejected 子命令。

策略同其它 migrate_*.py: 全有或全无, 显式 utf-8 读写并保留 CRLF。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "run.py"

# 默认目录: 需求明确"现阶段就固定读没打标签的", 但底层 scan_rejected_dirs
# 本身支持多目录传入, --dirs 允许覆盖/追加, 不锁死这一个目录。
DEFAULT_DIR = "没打标签的"

NEW_CMD = '''

def cmd_regen_rejected(args):
    """重新生成人工驳回样本(用原本那条参考缺陷), 落盘到 outputs/regenerated/。

    默认只反查 "没打标签的" 目录(--dirs 可传多个覆盖默认值)。反查基于文件名,
    不依赖 annotations.jsonl(人工挑出的驳回样本很可能在别的机器生成、标注文件
    不一定在手上)。--dry-run 只打印将要重生成的清单, 不发起任何 API 调用。
    """
    from src.config import load_config as _load_config
    from src.dataset import safe_name
    from src.generate import plan_regenerate_rejected, regenerate_rejected

    cfg = _load_config(args.config)
    dirs = args.dirs or [DEFAULT_REJECTED_DIR]
    dir_paths = [cfg.resolve(d) for d in dirs]

    tasks, scan_report, skipped = plan_regenerate_rejected(
        cfg, dir_paths, reroll_ref=args.reroll_ref)

    print(f"扫描目录: {[str(p) for p in scan_report.scanned_dirs]}")
    print(f"扫描文件数: {scan_report.scanned_files}")
    print(f"反查成功: {scan_report.ok_count}")
    print(f"反查失败: {scan_report.problem_count}")
    if scan_report.problems:
        print("失败详情(前10条):")
        for p in scan_report.problems[:10]:
            print(f"  - {p}")
    print(f"已重生成过(跳过): {len(skipped)}")
    print(f"本次将重生成: {len(tasks)} 张")

    if args.dry_run:
        if tasks:
            workers = max(1, int(cfg.api.get("max_workers", 3)))
            print(f"\\n预计 API 调用: 约 {len(tasks) * 2}~{len(tasks) * 4} 次"
                 f"(每张编辑+质检至少各一次, 视重试次数)")
            print(f"按单张 50~60 秒、并发 {workers} 估算, "
                 f"预计耗时 {len(tasks) * 50 / workers / 60:.0f}~"
                 f"{len(tasks) * 60 / workers / 60:.0f} 分钟")
            print("\\n清单(前 10 条):")
            for t in tasks[:10]:
                print(f"  {t.stem}")
                print(f"    干净图={t.clean_path.name}  参考={t.forced_ref['entry_id']}")
        print("\\n(试算, 未调用任何 API。确认无误后去掉 --dry-run 即开始)")
        return 0

    if not tasks:
        return 0

    regenerate_rejected(cfg, dir_paths, reroll_ref=args.reroll_ref)

    if not args.archive:
        return 0

    # 归档: 逐条检查目标产物是否真的落盘, 只归档确实生成成功的那些。
    # 必须在 regenerate_rejected 跑完之后才能做这个检查(落盘时间点已确定),
    # 且必须在归档之前完成, 不能颠倒 —— 挪走原文件后就没法从文件名反查了。
    regen_root = cfg.resolve(cfg.output.get("regenerated", "outputs/regenerated"))
    archived = 0
    for item in scan_report.resolved:
        matches = list((regen_root / safe_name(item.class_name)).glob(
            f"{item.original_stem}__r*.png")) if (
            regen_root / safe_name(item.class_name)).exists() else []
        if not matches:
            continue
        archive_dir = item.file_path.parent / "已重生成"
        archive_dir.mkdir(parents=True, exist_ok=True)
        dest = archive_dir / item.file_path.name
        if item.file_path.exists():
            item.file_path.rename(dest)
            archived += 1
    print(f"\\n已归档 {archived} 个原驳回文件到各目录下的 已重生成/ 子目录")
    return 0
'''

SUBPARSER = '''
    p = sub.add_parser(
        "regen-rejected",
        help="重新生成人工驳回样本(用原参考缺陷), 落盘到 outputs/regenerated/")
    p.add_argument("--dirs", nargs="+", default=None,
                   help=f"驳回样本所在目录, 可传多个; 默认 {DEFAULT_REJECTED_DIR!r}")
    p.add_argument("--dry-run", action="store_true",
                   help="只打印将要重生成的清单与预估成本, 不调用 API")
    p.add_argument("--reroll-ref", action="store_true",
                   help="换一条同类别的参考缺陷, 而不是沿用原参考"
                        "(适合怀疑是参考本身不合适的情形)")
    p.add_argument("--archive", action="store_true",
                   help="重生成成功后把原驳回文件移到各目录下的 已重生成/ 子目录")
    p.set_defaults(func=cmd_regen_rejected)

'''

CONST_DEF = f'\nDEFAULT_REJECTED_DIR = {DEFAULT_DIR!r}\n'


def main() -> int:
    with TARGET.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    edits: list[tuple[str, str, str]] = [
        (
            "模块顶部追加默认目录常量",
            'from app.runtime import bootstrap\n'
            'from src.config import load_config\n',

            'from app.runtime import bootstrap\n'
            'from src.config import load_config\n'
            + CONST_DEF,
        ),
        (
            "在 cmd_gen_ref 之后追加 cmd_regen_rejected",
            '    generate_with_reference(cfg, args.reference_entry, classes,\n'
            '                            per_class=args.per_class, resume=not args.force)\n',

            '    generate_with_reference(cfg, args.reference_entry, classes,\n'
            '                            per_class=args.per_class, resume=not args.force)\n'
            + NEW_CMD,
        ),
        (
            "注册 regen-rejected 子命令(放在 gen-ref 之后, worker 之前)",
            '    p.set_defaults(func=cmd_gen_ref)\n'
            '\n\n'
            '    p = sub.add_parser("worker",\n',

            '    p.set_defaults(func=cmd_gen_ref)\n'
            + SUBPARSER
            + '\n    p = sub.add_parser("worker",\n',
        ),
    ]

    problems: list[str] = []
    for label, old, _new in edits:
        n = text.count(old)
        if n != 1:
            problems.append(f"  [{label}] 命中 {n} 次(期望 1)")
    if problems:
        print("以下片段未精确命中, 未做任何修改:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    for label, old, new in edits:
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
