"""一次性迁移: 给 run.py 增加 gen-augment 子命令(需求二方式 A + 方式 C)。

策略同其它 migrate_*.py: 全有或全无, 显式 utf-8 读写并保留 CRLF。
待插入代码用三引号原样嵌入, 避免手写转义引号出错。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "run.py"

NEW_CMD = '''

def cmd_gen_augment(args):
    """定向补充: 按缺陷形态挑出参考条目, 批量补充训练样本。

    两种挑选方式(可组合, 取并集):
      方式A 按属性筛  --type/--severity/--count/--orientation/--ref-class
      方式C 按产出图反查 --from-images 目录  (从文件名反查它们用过哪些参考)

    默认强制预览: 打印匹配到的参考清单与成本预估后停下, 需要显式加 --yes 才真跑。
    """
    from src.catalog_query import QueryFilter, parse_range, query
    from src.config import load_config as _load_config
    from src.defect_catalog import load_catalog
    from src.generate import augment_by_references, plan_augment_by_references
    from src.rejection import extract_reference_ids

    cfg = _load_config(args.config)
    records = load_catalog(cfg)
    by_entry = {r["entry_id"]: r for r in records}

    selected: list = []          # 保持挑选顺序, 便于用户核对
    seen: set = set()

    def add(entry_id: str) -> None:
        if entry_id not in seen:
            seen.add(entry_id)
            selected.append(entry_id)

    # ---- 方式 C: 从出问题的产出图反查参考 ----
    if args.from_images:
        dirs = [cfg.resolve(d) for d in args.from_images]
        rep = extract_reference_ids(dirs, records)
        print(f"[方式C] 扫描目录: {[str(p) for p in rep.scanned_dirs]}")
        print(f"        文件数 {rep.scanned_files}, "
              f"反查出参考条目 {len(rep.entry_ids)} 条, "
              f"反查失败 {len(rep.problems)}")
        for p in rep.problems[:5]:
            print(f"        - {p}")
        # 按 entry_id 排序保证多次运行结果稳定(set 迭代顺序不保证)
        for eid in sorted(rep.entry_ids):
            add(eid)

    # ---- 方式 A: 按属性筛 ----
    used_attr_filter = any([args.type, args.severity, args.count,
                           args.orientation, args.ref_class])
    if used_attr_filter:
        sev_min, sev_max = parse_range(args.severity)
        cnt_min, cnt_max = parse_range(args.count)
        flt = QueryFilter(
            defect_type=args.type, severity_min=sev_min, severity_max=sev_max,
            count_min=cnt_min, count_max=cnt_max,
            orientation_kw=args.orientation, class_name=args.ref_class)
        result = query(records, flt)
        print(f"[方式A] 按属性筛选: {result.count}/{result.total} 条命中")
        dist = result.distribution()
        print(f"        按类型: {dist['by_type']}")
        print(f"        按严重度: {dist['by_severity']}")
        print(f"        按来源类别: {dist['by_class']}")
        for r in result.matched:
            add(r["entry_id"])

    # ---- 显式指定 entry_id ----
    if args.refs:
        for eid in args.refs:
            if eid not in by_entry:
                print(f"[error] 缺陷库中找不到参考条目: {eid}")
                return 2
            add(eid)

    if not selected:
        print("没有选中任何参考条目。请至少给出一种挑选方式:")
        print("  --from-images <目录>        从出问题的产出图反查用过的参考")
        print("  --severity 4-5 等属性条件   按形态属性筛选")
        print("  --refs <entry_id> ...      直接指定参考条目")
        return 2

    if args.max_refs and len(selected) > args.max_refs:
        print(f"\\n[限制] 选中 {len(selected)} 条, 按 --max-refs 截断为前 "
              f"{args.max_refs} 条")
        selected = selected[:args.max_refs]

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    print(f"\\n最终选中参考条目 {len(selected)} 条, 目标类别 {classes}, "
          f"每条每类 {args.per_ref} 张")
    print("\\n参考条目清单(前 15 条):")
    for eid in selected[:15]:
        r = by_entry[eid]
        print(f"  {eid}")
        print(f"    类型={r.get('defect_type')} 严重度={r.get('severity')} "
              f"条数={r.get('count')} 走向={r.get('orientation','')[:24]}")

    tasks = plan_augment_by_references(cfg, selected, classes,
                                      per_ref=args.per_ref,
                                      catalog_records=records)
    workers = max(1, int(cfg.api.get("max_workers", 3)))
    print(f"\\n将生成 {len(tasks)} 张样本(落进 outputs/generated/, "
          f"质检不合格的照常进 outputs/rejected/)")
    print(f"预计 API 调用约 {len(tasks) * 2}~{len(tasks) * 4} 次"
          f"(每张编辑+质检至少各一次, 视重试次数)")
    print(f"按单张 50~60 秒、并发 {workers} 估算, 预计耗时 "
          f"{len(tasks) * 50 / workers / 60:.0f}~"
          f"{len(tasks) * 60 / workers / 60:.0f} 分钟")

    if not args.yes:
        print("\\n(以上为预览, 未调用任何 API。确认无误后加 --yes 开始生成)")
        return 0

    augment_by_references(cfg, selected, classes, per_ref=args.per_ref,
                          resume=not args.force)
    return 0
'''

SUBPARSER = '''
    p = sub.add_parser(
        "gen-augment",
        help="定向补充: 按缺陷形态挑参考条目批量补样本(默认只预览, 加 --yes 才跑)")
    p.add_argument("--classes", required=True,
                   help="目标类别, 逗号分隔, 如 1.jpg,1.bmp")
    p.add_argument("--per-ref", type=int, default=1,
                   help="每条参考在每个类别上生成多少张(默认1)")
    p.add_argument("--from-images", nargs="+", default=None,
                   help="[方式C] 出问题的产出图所在目录, 从文件名反查用过的参考")
    p.add_argument("--type", default=None,
                   help="[方式A] 缺陷类型精确匹配, 如 变形 或 划痕")
    p.add_argument("--severity", default=None,
                   help="[方式A] 严重度区间, 如 4-5 或 4")
    p.add_argument("--count", default=None,
                   help="[方式A] 褶皱条数区间, 如 5-12")
    p.add_argument("--orientation", default=None,
                   help="[方式A] 走向关键词包含匹配, 如 横向 / 斜向 / 交叉")
    p.add_argument("--ref-class", default=None,
                   help="[方式A] 限定参考缺陷的来源类别, 如 5.1")
    p.add_argument("--refs", nargs="+", default=None,
                   help="直接指定参考条目 entry_id, 可多个")
    p.add_argument("--max-refs", type=int, default=None,
                   help="选中过多时只取前 N 条(控制成本)")
    p.add_argument("--yes", action="store_true",
                   help="确认执行(不加则只预览清单与成本, 不调用 API)")
    p.add_argument("--force", action="store_true", help="忽略断点续跑")
    p.set_defaults(func=cmd_gen_augment)

'''


def main() -> int:
    with TARGET.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    edits: list[tuple[str, str, str]] = [
        (
            "在 cmd_regen_rejected 之后追加 cmd_gen_augment",
            '    print(f"\\n已归档 {archived} 个原驳回文件到各目录下的 已重生成/ 子目录")\n'
            '    return 0\n',

            '    print(f"\\n已归档 {archived} 个原驳回文件到各目录下的 已重生成/ 子目录")\n'
            '    return 0\n'
            + NEW_CMD,
        ),
        (
            "注册 gen-augment 子命令(放在 regen-rejected 之后)",
            '    p.set_defaults(func=cmd_regen_rejected)\n',

            '    p.set_defaults(func=cmd_regen_rejected)\n'
            + SUBPARSER,
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
