"""一次性迁移: 给 run.py 增加 find-similar 子命令, 并给 gen-augment 接入方式 B。

前置条件: tools/check_morphology.py 的两道门槛已通过(自检 25/25、走向命中率
0.863 vs 基线 0.724、类型一致率 0.932 vs 0.866、条数偏差 3.42 优于随机 4.92)。
门槛未过时不应执行本迁移 —— 方式 B 应保持不可用, 让用户继续用方式 A/C。

策略同其它 migrate_*.py: 全有或全无, 显式 utf-8 读写并保留 CRLF。
待插入代码用三引号原样嵌入, 避免手写转义引号出错。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "run.py"

FIND_SIMILAR_CMD = '''

def cmd_find_similar(args):
    """按褶皱形态检索缺陷库: 给一张出问题的图或一条种子参考, 找形态最像的条目。

    完全离线, 不调用任何 API。基于 structure_ref 的灰度浮雕图算形态描述子
    (方向直方图/多尺度带通能量/连通域统计/方向集中度), 首次运行会为缺陷库全部
    裁剪图建一次缓存(约 1 分钟), 之后检索是毫秒级。

    输出的 entry_id 可以直接喂给 gen-augment --refs 做定向补充。
    """
    from src import morphology
    from src.config import load_config as _load_config
    from src.defect_catalog import load_catalog

    cfg = _load_config(args.config)
    records = load_catalog(cfg)

    if not args.image and not args.entry:
        print("请给出检索种子, 二者其一:")
        print("  --image <图片路径>   出问题的图(误检区域截图/漏检的真实褶皱)")
        print("  --entry <entry_id>  以缺陷库里已有条目为种子找相似")
        return 2

    if args.rebuild_cache:
        print("重建形态描述子缓存(遍历缺陷库全部裁剪图, 约 1 分钟) ...")

    if args.image:
        image_path = cfg.resolve(args.image)
        if not image_path.exists():
            print(f"[error] 图片不存在: {image_path}")
            return 2
        print(f"检索种子(图片): {image_path}")
        hits = morphology.query_by_image(
            cfg, image_path, records, top_k=args.top_k,
            rebuild_cache=args.rebuild_cache, progress=True)
    else:
        by_id = {r["entry_id"]: r for r in records}
        if args.entry not in by_id:
            print(f"[error] 缺陷库中找不到条目: {args.entry}")
            return 2
        seed = by_id[args.entry]
        print(f"检索种子(条目): {args.entry}")
        print(f"  类型={seed.get('defect_type')} 严重度={seed.get('severity')} "
              f"条数={seed.get('count')} 走向={seed.get('orientation','')}")
        hits = morphology.query_by_entry(
            cfg, args.entry, records, top_k=args.top_k,
            rebuild_cache=args.rebuild_cache)

    if not hits:
        print("没有检索到任何结果(缺陷库描述子可能为空)")
        return 1

    print(f"\\n形态最相似的 {len(hits)} 条(距离越小越像):")
    for i, h in enumerate(hits, 1):
        r = h.record
        print(f"{i:>3}. d={h.distance:.3f}  {h.entry_id}")
        print(f"      类型={r.get('defect_type')} 严重度={r.get('severity')} "
              f"条数={r.get('count')} 走向={str(r.get('orientation',''))[:40]}")

    print("\\n可直接把这些 entry_id 交给 gen-augment 做定向补充, 例如:")
    ids_preview = " ".join(h.entry_id for h in hits[:3])
    print(f"  run.py gen-augment --classes 1.jpg --per-ref 2 --refs {ids_preview}")
    print("或者一步到位(gen-augment 内置同样的检索):")
    seed_arg = (f"--similar-to-image {args.image}" if args.image
                else f"--similar-to-entry {args.entry}")
    print(f"  run.py gen-augment --classes 1.jpg --per-ref 2 "
          f"{seed_arg} --similar-top-k {args.top_k}")
    return 0
'''

FIND_SIMILAR_SUBPARSER = '''
    p = sub.add_parser(
        "find-similar",
        help="按褶皱形态检索缺陷库(离线, 不调API), 结果可喂给 gen-augment")
    p.add_argument("--image", default=None,
                   help="检索种子图片: 出问题的图(误检截图/漏检的真实褶皱)")
    p.add_argument("--entry", default=None,
                   help="检索种子条目: 以缺陷库里已有 entry_id 为种子找相似")
    p.add_argument("--top-k", type=int, default=15, help="返回前 N 条(默认15)")
    p.add_argument("--rebuild-cache", action="store_true",
                   help="强制重建形态描述子缓存(改过描述子参数或缺陷库扩容后用)")
    p.set_defaults(func=cmd_find_similar)

'''

# gen-augment 里插入方式 B 的挑选逻辑。锚点是方式 A 之前的注释行。
METHOD_B_ANCHOR = '''    # ---- 方式 A: 按属性筛 ----
'''

METHOD_B_CODE = '''    # ---- 方式 B: 按形态相似检索 ----
    if args.similar_to_image or args.similar_to_entry:
        from src import morphology
        if args.similar_to_image:
            seed_path = cfg.resolve(args.similar_to_image)
            if not seed_path.exists():
                print(f"[error] 种子图片不存在: {seed_path}")
                return 2
            hits = morphology.query_by_image(
                cfg, seed_path, records, top_k=args.similar_top_k,
                progress=True)
            print(f"[方式B] 按形态相似检索(种子图片 {seed_path.name}): "
                  f"取前 {len(hits)} 条")
        else:
            if args.similar_to_entry not in by_entry:
                print(f"[error] 缺陷库中找不到种子条目: {args.similar_to_entry}")
                return 2
            hits = morphology.query_by_entry(
                cfg, args.similar_to_entry, records, top_k=args.similar_top_k)
            print(f"[方式B] 按形态相似检索(种子条目 {args.similar_to_entry}): "
                  f"取前 {len(hits)} 条")
        for h in hits:
            print(f"        d={h.distance:.3f}  {h.entry_id}")
            add(h.entry_id)

'''

METHOD_B_SUBPARSER_ANCHOR = '''    p.add_argument("--refs", nargs="+", default=None,
                   help="直接指定参考条目 entry_id, 可多个")
'''

METHOD_B_SUBPARSER = '''    p.add_argument("--similar-to-image", default=None,
                   help="[方式B] 以一张出问题的图为种子, 检索形态相似的参考条目")
    p.add_argument("--similar-to-entry", default=None,
                   help="[方式B] 以缺陷库某条目为种子, 检索形态相似的参考条目")
    p.add_argument("--similar-top-k", type=int, default=10,
                   help="[方式B] 相似检索取前 N 条(默认10)")
'''

# 无挑选方式时的提示也要带上方式 B
HINT_ANCHOR = '''        print("  --severity 4-5 等属性条件   按形态属性筛选")
'''
HINT_NEW = '''        print("  --similar-to-image <图>     按形态相似检索(需要一张出问题的图)")
        print("  --similar-to-entry <id>     以缺陷库某条目为种子按形态相似检索")
        print("  --severity 4-5 等属性条件   按形态属性筛选")
'''


def main() -> int:
    with TARGET.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    edits: list[tuple[str, str, str]] = [
        (
            "在 cmd_gen_augment 之前追加 cmd_find_similar",
            '\ndef cmd_gen_augment(args):\n',
            FIND_SIMILAR_CMD + '\n\ndef cmd_gen_augment(args):\n',
        ),
        (
            "gen-augment 接入方式 B 的挑选逻辑",
            METHOD_B_ANCHOR,
            METHOD_B_CODE + METHOD_B_ANCHOR,
        ),
        (
            "gen-augment 增加方式 B 的参数",
            METHOD_B_SUBPARSER_ANCHOR,
            METHOD_B_SUBPARSER + METHOD_B_SUBPARSER_ANCHOR,
        ),
        (
            "无挑选方式时的提示补上方式 B",
            HINT_ANCHOR,
            HINT_NEW,
        ),
        (
            "注册 find-similar 子命令(放在 gen-augment 之后)",
            '    p.set_defaults(func=cmd_gen_augment)\n',
            '    p.set_defaults(func=cmd_gen_augment)\n' + FIND_SIMILAR_SUBPARSER,
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
