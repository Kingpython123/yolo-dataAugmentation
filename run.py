"""缺陷合成流水线 CLI 入口。

用法:
  python run.py test-api                    # 测试中转站连通性(视觉+编辑模型)
  python run.py selftest                    # 离线自检(不调用API, 验证分割/差分/回贴)
  python run.py build-catalog [--max-per-class N] [--overwrite]
  python run.py generate [--limit-per-class N] [--classes 1.jpg,2.jpg]
  python run.py requalify [--apply]        # 按新阈值离线重判已有样本(免费)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import load_config


def cmd_test_api(args):
    from src.api_client import RelayClient
    from PIL import Image
    cfg = load_config(args.config)
    relay = RelayClient(cfg)
    print(f"base_url = {relay.base_url}")

    print("\n[1/2] 测试视觉模型 ...")
    img = Image.new("RGB", (64, 64), (120, 120, 120))
    try:
        txt = relay.chat_vision("用一句话描述这张图的主色。", images=[img])
        print("  OK ->", txt[:120])
    except Exception as e:
        print("  失败:", e)

    print("\n[2/2] 测试图像编辑模型 ...")
    try:
        out = relay.edit_image(
            "Add a tiny realistic dark scratch in the center. Keep everything else identical.",
            img)
        print(f"  OK -> 返回图像 size={out.size}")
    except Exception as e:
        print("  失败:", e)


def cmd_selftest(args):
    """不调用API: 用合成图验证 分割/裁块/差分/回贴/像素校验 是否正常。"""
    import numpy as np
    from PIL import Image, ImageDraw
    from src import mask_utils

    cfg = load_config(args.config)
    # 造一张"瓶子": 黑底 + 中间亮竖条
    W, H = 600, 1200
    arr = np.full((H, W, 3), 20, np.uint8)
    arr[100:1100, 200:400] = 180
    clean = Image.fromarray(arr)

    bottle = mask_utils.segment_bottle(clean)
    frac = (bottle > 0).mean()
    print(f"分割前景占比 = {frac:.3f} (期望 ~0.28)")

    import random
    box = mask_utils.pick_patch_box(bottle, 256, random.Random(0))
    print("裁块 box =", box)
    orig_patch = mask_utils.crop(clean, box)

    # 模拟编辑: 在裁块上画一道划痕
    edited = orig_patch.copy()
    d = ImageDraw.Draw(edited)
    d.line([(40, 40), (200, 210)], fill=(60, 40, 40), width=5)

    pmask = mask_utils.diff_defect_mask(orig_patch, edited)
    print(f"差分掩膜变化比例 = {mask_utils.changed_ratio(pmask):.4f}")

    result = mask_utils.feather_composite(clean, edited, pmask, box, feather=6)
    full_mask = mask_utils.full_mask_from_patch(clean.size, pmask, box)
    bg = mask_utils.background_change_ratio(
        clean, result, np.array(full_mask.convert("L")))
    print(f"掩膜外像素变化比例 = {bg:.5f} (硬约束, 应接近0)")

    out_dir = cfg.out_path("root") / "selftest"
    out_dir.mkdir(parents=True, exist_ok=True)
    clean.save(out_dir / "clean.png")
    result.save(out_dir / "result.png")
    full_mask.save(out_dir / "mask.png")
    print(f"\n[OK] 自检产物已保存到 {out_dir}")
    print("流水线的图像处理环节工作正常。填好 config.yaml 的 API 后即可 test-api / build-catalog / generate。")


def cmd_inspect(args):
    """离线体检缺陷库: 格式/类型/严重度/字段完整度/裁剪图是否存在。"""
    import json
    from collections import Counter
    from pathlib import Path as P
    cfg = load_config(args.config)
    f = cfg.out_path("catalog") / "catalog.json"
    if not f.exists():
        print(f"缺陷库不存在: {f}")
        return
    recs = json.loads(f.read_text(encoding="utf-8"))
    print(f"缺陷库: {f}\n条目总数: {len(recs)}")

    new = [r for r in recs if "severity" in r]
    print(f"新格式(含细化字段): {len(new)} / 旧格式: {len(recs) - len(new)}")
    print("按类型:", dict(Counter(r.get("defect_type") for r in recs)))
    print("按严重度:", dict(sorted(Counter(
        r.get("severity", "?") for r in recs).items(), key=lambda x: str(x[0]))))
    print("按类别:", dict(sorted(Counter(r.get("class_name") for r in recs).items())))

    white = (cfg.get("catalog", {}) or {}).get("defect_types", ["变形", "划痕"])
    bad_type = [r["entry_id"] for r in recs if r.get("defect_type") not in white]
    missing = [r["entry_id"] for r in recs
               if not cfg.resolve(str(r.get("crop_path", ""))).exists()]
    no_size = [r["entry_id"] for r in recs
               if not isinstance(r.get("source_size"), (list, tuple))]
    fields = ["orientation", "geometry", "extent", "photometry",
              "edge_profile", "texture_effect", "prompt_hint"]
    empty = [r["entry_id"] for r in recs
             if sum(1 for k in fields if not str(r.get(k, "")).strip()) > 2]
    print(f"\n非白名单类型: {len(bad_type)}", bad_type[:5])
    print(f"裁剪图缺失: {len(missing)}", missing[:5])
    if no_size:
        print(f"[警告] 缺 source_size 的条目: {len(no_size)} 条 —— 这些条目在"
              f"没有'有缺陷'原图的机器上会退回按绝对像素算裁块(尺寸偏大)")
    print(f"描述字段大量缺失: {len(empty)}", empty[:5])

    big = [(r["entry_id"], r["bbox"][2], r["bbox"][3]) for r in recs
           if r.get("bbox") and max(r["bbox"][2], r["bbox"][3])
           > cfg.generation.get("max_patch_size", 1536)]
    if big:
        print(f"\n[提示] {len(big)} 条缺陷长边超过 max_patch_size, 裁块无法完整覆盖:")
        for e, w, h in big[:5]:
            print(f"   {e}  bbox={w}x{h}")


def cmd_debug_preview(args):
    """离线拼图: 把某批 debug 中间产物按类别/尝试拼成一张对比图, 供人工查看。

    不调用API, 纯读取 outputs/debug 下已有的中间产物(0原图参考/1目标裁块/
    2模型原始输出/3光度对齐后/4差分掩膜)。可选 --recompute 用当前 config
    的差分阈值/面积上限重算掩膜, 与原掩膜并排对比, 用于验证参数改动效果。
    """
    import json
    from pathlib import Path as P

    import numpy as np
    from PIL import Image, ImageDraw

    from src import mask_utils
    from src.dataset import safe_name

    cfg = load_config(args.config)
    only = set(c.strip() for c in args.classes.split(",")) if args.classes else None
    dirs = sorted(d for d in (cfg.out_path("root") / "debug").rglob(f"*{args.tag}*")
                  if d.is_dir())
    if only:
        dirs = [d for d in dirs if d.parent.name in only]
    if not dirs:
        print(f"没有匹配 tag={args.tag!r} classes={args.classes!r} 的 debug 目录")
        return

    out_dir = cfg.out_path("root") / "debug_preview"
    out_dir.mkdir(parents=True, exist_ok=True)
    th = args.thumb

    def load(p: Path):
        return Image.open(p).convert("RGB") if p.exists() else None

    def label_panel(img: Image.Image, text: str) -> Image.Image:
        w = max(1, int(img.width * th / img.height))
        img = img.resize((w, th))
        canvas = Image.new("RGB", (w, th + 16), (255, 255, 255))
        canvas.paste(img, (0, 16))
        ImageDraw.Draw(canvas).text((2, 1), text, fill=(0, 0, 0))
        return canvas

    def mask_overlay(base: Image.Image, mask: np.ndarray, color=(255, 0, 0)):
        arr = np.array(base).astype(np.float32)
        a = (mask > 0).astype(np.float32)[..., None] * 0.5
        tint = np.zeros_like(arr)
        tint[..., 0], tint[..., 1], tint[..., 2] = color
        return Image.fromarray(np.clip(arr * (1 - a) + tint * a, 0, 255)
                               .astype(np.uint8))

    rows_all = []
    print(f"{'类别':7} {'尝试':4} {'裁块':10} {'旧掩膜占比':>9}"
          + (f" {'新掩膜占比':>9}" if args.recompute else ""))
    for d in dirs:
        cls = d.parent.name
        metas = sorted(d.glob("*_meta.json"))
        for meta_path in metas:
            tag = meta_path.name.split("_")[0]
            j = json.loads(meta_path.read_text(encoding="utf-8"))
            ref = load(d / f"{tag}_0a_reference.png") or load(d / f"{tag}_0_reference.png")
            struct = load(d / f"{tag}_0b_structure.png")
            orig = load(d / f"{tag}_1_orig_patch.png")
            raw = load(d / f"{tag}_2_model_raw.png")
            aligned = load(d / f"{tag}_3_aligned.png")
            old_mask_p = (d / f"{tag}_4b_mask_label.png")
            old_mask = (np.array(load(old_mask_p).convert("L"))
                       if old_mask_p.exists() else
                       (np.array(load(d / f"{tag}_4_mask.png").convert("L"))
                        if (d / f"{tag}_4_mask.png").exists() else None))
            if orig is None or aligned is None:
                continue

            panels = []
            if ref is not None:
                panels.append(label_panel(ref, "参考缺陷"))
            if struct is not None:
                panels.append(label_panel(struct, "结构浮雕图"))
            panels.append(label_panel(orig, "目标裁块(合成前)"))
            if raw is not None:
                panels.append(label_panel(raw, "模型原始输出"))
            panels.append(label_panel(aligned, "光度对齐后"))

            gen = cfg.generation
            new_ratio = None
            if old_mask is not None:
                panels.append(label_panel(
                    mask_overlay(aligned, old_mask, (255, 0, 0)),
                    f"旧掩膜(占比{(old_mask > 0).mean():.3f})"))
            if args.recompute:
                pm = mask_utils.diff_defect_mask(
                    orig, aligned, thresh=gen.get("diff_threshold", 14),
                    max_area_ratio=gen.get("max_defect_area_ratio", 0.45))
                new_ratio = (pm > 0).mean()
                panels.append(label_panel(
                    mask_overlay(aligned, pm, (0, 0, 255)),
                    f"重算掩膜(占比{new_ratio:.3f})"))

            row = Image.new("RGB", (sum(p.width for p in panels) + 8 * (len(panels) - 1),
                                   th + 16), (255, 255, 255))
            x = 0
            for pnl in panels:
                row.paste(pnl, (x, 0))
                x += pnl.width + 8
            head = Image.new("RGB", (row.width, 20), (240, 240, 240))
            ImageDraw.Draw(head).text(
                (4, 3), f"{cls} / {tag}  patch_box={j.get('patch_box')}  "
                       f"realism={j.get('qc', {}).get('realism')}", fill=(0, 0, 0))
            combo = Image.new("RGB", (row.width, row.height + 20), (255, 255, 255))
            combo.paste(head, (0, 0))
            combo.paste(row, (0, 20))
            fname = f"{safe_name(cls)}_{tag}.png"
            combo.save(out_dir / fname)
            rows_all.append(combo)

            old_r = (old_mask > 0).mean() if old_mask is not None else float("nan")
            extra = f" {new_ratio:>9.3f}" if new_ratio is not None else ""
            print(f"{cls:7} {tag:4} {str(j.get('patch_box')):10} "
                  f"{old_r:>9.3f}{extra}")

    if rows_all:
        W = max(r.width for r in rows_all)
        sheet = Image.new("RGB", (W, sum(r.height for r in rows_all) + 4 * len(rows_all)),
                          (200, 200, 200))
        y = 0
        for r in rows_all:
            sheet.paste(r, (0, y))
            y += r.height + 4
        sheet.save(out_dir / "_all.png")
    print(f"\n共 {len(rows_all)} 张(类别x尝试)。预览已保存: {out_dir}")
    print("先看 _all.png, 或单张 outputs/debug_preview/<类别>_<尝试>.png")


def cmd_seg_check(args):
    """离线对比两种瓶身分割, 输出叠加预览供人工确认(不调用API)。"""
    import numpy as np
    from PIL import Image, ImageDraw
    from src import mask_utils
    from src.dataset import safe_name, scan_class_images

    cfg = load_config(args.config)
    clean = scan_class_images(cfg, cfg.clean_root())
    if args.classes:
        only = [c.strip() for c in args.classes.split(",")]
        clean = {k: v for k, v in clean.items() if k in only}
    out_dir = cfg.out_path("root") / "seg_preview"
    out_dir.mkdir(parents=True, exist_ok=True)

    def overlay(im: Image.Image, mask: np.ndarray, color) -> Image.Image:
        base = np.array(im.convert("RGB")).astype(np.float32)
        tint = np.zeros_like(base)
        tint[..., 0], tint[..., 1], tint[..., 2] = color
        a = (mask > 0).astype(np.float32)[..., None] * 0.45
        return Image.fromarray(np.clip(base * (1 - a) + tint * a, 0, 255)
                               .astype(np.uint8))

    rows = []
    print(f"{'类别':8} {'otsu占比':>9} {'otsu宽':>7} {'flood占比':>10} {'flood宽':>8}")
    for cls, paths in sorted(clean.items()):
        p = paths[args.index % len(paths)]
        im = Image.open(p).convert("RGB")
        roi = (cfg.data.get("bottle_roi") or {}).get(cls)
        mo = mask_utils.segment_bottle_otsu(im)
        mf = mask_utils.segment_bottle(im, roi=roi)
        bo, bf = mask_utils.mask_bbox(mo), mask_utils.mask_bbox(mf)
        print(f"{cls:8} {(mo > 0).mean():>9.3f} {(bo.w if bo else 0):>7} "
              f"{(mf > 0).mean():>10.3f} {(bf.w if bf else 0):>8}"
              f"   ROI={roi if roi else '整图'}")

        if args.split:
            # 把泛洪前景按亮度一分为二, 用于判断桌布落在亮的一侧还是暗的一侧
            import cv2
            g = np.array(im.convert("L"))
            inside = mf > 0
            t = int(np.median(g[inside])) if inside.any() else 128
            bright = ((g > t) & inside).astype(np.uint8) * 255
            dark = ((g <= t) & inside).astype(np.uint8) * 255
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
            bright = cv2.morphologyEx(bright, cv2.MORPH_OPEN, k)
            dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, k)
            panels = [im, overlay(im, bright, (255, 0, 0)),
                      overlay(im, dark, (0, 0, 255))]
            label = f"{cls}   原图 | 前景中较亮的一半(红, 阈值{t}) | 较暗的一半(蓝)"
        else:
            panels = [im, overlay(im, mo, (0, 255, 0)),
                      overlay(im, mf, (255, 0, 0))]
            label = (f"{cls}   原图 | 旧Otsu(绿) | 当前分割(红, "
                     f"ROI={roi if roi else '整图'})")
        th = 420
        panels = [q.resize((max(1, int(q.width * th / q.height)), th))
                  for q in panels]
        if args.grid:  # 叠比例网格, 便于人工读出 ROI 坐标
            for q in panels:
                d = ImageDraw.Draw(q)
                for i in range(1, 10):
                    gx, gy = int(q.width * i / 10), int(q.height * i / 10)
                    d.line([(gx, 0), (gx, q.height)], fill=(255, 255, 0), width=1)
                    d.line([(0, gy), (q.width, gy)], fill=(255, 255, 0), width=1)
                    d.text((gx + 2, 2), f".{i}", fill=(255, 255, 0))
                    d.text((2, gy + 1), f".{i}", fill=(255, 255, 0))

        row = Image.new("RGB", (sum(q.width for q in panels) + 20, th + 18),
                        (255, 255, 255))
        x = 0
        for q in panels:
            row.paste(q, (x, 18))
            x += q.width + 10
        ImageDraw.Draw(row).text((2, 3), label, fill=(0, 0, 0))
        suffix = "_split" if args.split else ""
        row.save(out_dir / f"{safe_name(cls)}{suffix}.png")
        rows.append(row)

    if rows:
        W = max(r.width for r in rows)
        sheet = Image.new("RGB", (W, sum(r.height for r in rows)), (255, 255, 255))
        y = 0
        for r in rows:
            sheet.paste(r, (0, y))
            y += r.height
        sheet.save(out_dir / ("_all_split.png" if args.split else "_all.png"))
    print(f"\n预览已保存: {out_dir}  (先看 _all.png)")


def cmd_requalify(args):
    from src.requalify import requalify
    cfg = load_config(args.config)
    only = [c.strip() for c in args.classes.split(",")] if args.classes else None
    requalify(cfg, apply=args.apply, only_classes=only)


def cmd_build_catalog(args):
    from src.defect_catalog import build_catalog, load_analyzed
    cfg = load_config(args.config)
    only = args.classes.split(",") if args.classes else None

    if args.dry_run:
        import json as _json
        from src.dataset import scan_class_images
        cat = cfg.out_path("catalog") / "catalog.json"
        recs = (_json.loads(cat.read_text(encoding="utf-8"))
                if cat.exists() else [])
        analyzed = load_analyzed(cfg) if recs and not args.overwrite else set()
        imgs = scan_class_images(cfg, cfg.defect_root())
        if only:
            imgs = {k: v for k, v in imgs.items() if k in only}
        todo_total = 0
        print(f"{'类别':8} {'总图数':>7} {'已分析':>7} {'本次待分析':>10}")
        for cls in sorted(imgs):
            paths = imgs[cls]
            done = sum(1 for p in paths if str(p.resolve()) in analyzed)
            todo = len(paths) - done
            if args.max_per_class:
                todo = min(todo, args.max_per_class)
            todo_total += todo
            print(f"{cls:8} {len(paths):>7} {done:>7} {todo:>10}")
        rate = (len(recs) / max(1, len(analyzed))) if analyzed else 1.36
        print(f"\n本次将调用视觉模型 {todo_total} 次(每张图一次)")
        print(f"按当前产出率 {rate:.2f} 条/图估算, 预计新增约 "
              f"{int(todo_total * rate)} 条, 总量约 "
              f"{len(recs) + int(todo_total * rate)} 条")
        workers = max(1, int(cfg.api.get("max_workers", 3)))
        print(f"并发 {workers}, 按单次 15~30 秒估算, 预计耗时 "
              f"{todo_total * 15 / workers / 60:.0f}~"
              f"{todo_total * 30 / workers / 60:.0f} 分钟")
        print("\n(试算, 未调用任何 API。确认后去掉 --dry-run 即开始)")
        return

    build_catalog(cfg, max_per_class=args.max_per_class,
                  overwrite=args.overwrite, only_classes=only)


def cmd_generate(args):
    from src.generate import generate
    cfg = load_config(args.config)
    only = args.classes.split(",") if args.classes else None
    generate(cfg, limit_per_class=args.limit_per_class, only_classes=only,
             resume=not args.force)


def cmd_gen_target(args):
    from src.generate import generate_target
    cfg = load_config(args.config)
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    generate_target(cfg, classes, count=args.count, resume=not args.force)


def cmd_gen_sweep(args):
    from src.generate import generate_sweep
    cfg = load_config(args.config)
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    generate_sweep(cfg, classes, resume=not args.force,
                   shuffle=args.shuffle, max_refs=args.max_refs)


def cmd_gen_ref(args):
    from src.generate import generate_with_reference
    cfg = load_config(args.config)
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    generate_with_reference(cfg, args.reference_entry, classes,
                            per_class=args.per_class, resume=not args.force)


def main():
    ap = argparse.ArgumentParser(description="工业瓶子缺陷合成流水线")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("test-api").set_defaults(func=cmd_test_api)
    sub.add_parser("selftest").set_defaults(func=cmd_selftest)
    sub.add_parser("inspect", help="离线体检缺陷库(不调用API)").set_defaults(
        func=cmd_inspect)

    p = sub.add_parser("debug-preview",
                       help="离线拼图查看 outputs/debug 中间产物(不调用API)")
    p.add_argument("--tag", type=str, default="",
                   help="按 debug 目录名过滤(如某个 reference_entry 的片段)")
    p.add_argument("--classes", type=str, default=None, help="仅看指定类别, 逗号分隔")
    p.add_argument("--recompute", action="store_true",
                   help="用当前 config 的差分阈值/面积上限重算掩膜并对比")
    p.add_argument("--thumb", type=int, default=420, help="每格缩略图高度(像素)")
    p.set_defaults(func=cmd_debug_preview)

    p = sub.add_parser("seg-check",
                       help="离线对比两种瓶身分割并输出叠加预览(不调用API)")
    p.add_argument("--classes", type=str, default=None, help="仅检查指定类别")
    p.add_argument("--index", type=int, default=0, help="每类取第几张图(默认0)")
    p.add_argument("--split", action="store_true",
                   help="改为把泛洪前景按亮度一分为二, 用于判断桌布在亮侧还是暗侧")
    p.add_argument("--grid", action="store_true",
                   help="叠加 0.1 步长的比例网格, 便于读出 bottle_roi 坐标")
    p.set_defaults(func=cmd_seg_check)

    p = sub.add_parser("requalify",
                       help="按当前阈值离线重判已生成样本(零API成本), 默认试运行")
    p.add_argument("--apply", action="store_true",
                   help="真正落地: 移动图/掩膜并重写 annotations.jsonl(先自动备份)")
    p.add_argument("--classes", type=str, default=None,
                   help="仅复判指定类别, 逗号分隔")
    p.set_defaults(func=cmd_requalify)

    p = sub.add_parser(
        "build-catalog",
        help="编目缺陷库。已有库时默认增量(只分析没分析过的图), 结果追加")
    p.add_argument("--max-per-class", type=int, default=None,
                   help="每类最多分析多少张缺陷图(控制成本)")
    p.add_argument("--dry-run", action="store_true",
                   help="只试算规模/成本/耗时, 不调用 API")
    p.add_argument("--overwrite", action="store_true",
                   help="全量重建(丢弃现有库并重新分析所有图, 慎用)")
    p.add_argument("--classes", type=str, default=None,
                   help="仅(重)建指定类别, 逗号分隔, 如 5.1,5.2,5.3,5.4")
    p.set_defaults(func=cmd_build_catalog)

    p = sub.add_parser("generate")
    p.add_argument("--limit-per-class", type=int, default=None,
                   help="每类最多处理多少张干净图")
    p.add_argument("--classes", type=str, default=None,
                   help="仅处理指定类别, 逗号分隔, 如 1.jpg,2.jpg")
    p.add_argument("--force", action="store_true",
                   help="忽略断点续跑, 重新生成已完成的样本")
    p.set_defaults(func=cmd_generate)

    p = sub.add_parser(
        "gen-target",
        help="给指定类别各生成固定数量的样本, 参考库按顺序循环使用以保证全覆盖")
    p.add_argument("--classes", required=True,
                   help="目标类别, 逗号分隔, 如 1.jpg 或 1.jpg,1.bmp,5.1")
    p.add_argument("--count", type=int, required=True,
                   help="每个类别各生成多少张(达到参考库条数才算覆盖全部参考)")
    p.add_argument("--force", action="store_true", help="忽略断点续跑")
    p.set_defaults(func=cmd_gen_target)

    p = sub.add_parser(
        "gen-sweep",
        help="每条参考缺陷只用一次, 一图一参考, 按类别顺序铺开(用完溢出到下一类)")
    p.add_argument("--classes", required=True,
                   help="目标类别, 按顺序逗号分隔, 如 5.1,5.2")
    p.add_argument("--shuffle", action="store_true",
                   help="打乱参考顺序(默认按缺陷库原顺序)")
    p.add_argument("--max-refs", type=int, default=None,
                   help="只用前 N 条参考(小规模试跑用)")
    p.add_argument("--force", action="store_true", help="忽略断点续跑")
    p.set_defaults(func=cmd_gen_sweep)

    p = sub.add_parser("gen-ref", help="定向: 固定某条参考缺陷, 在指定类别各随机抽N张生成")
    p.add_argument("--reference-entry", required=True,
                   help="参考缺陷的 entry_id, 如 1.jpg__1_20260426161537754__0")
    p.add_argument("--classes", required=True,
                   help="目标类别, 逗号分隔, 如 1.jpg,2.jpg,3.jpg,4.jpg")
    p.add_argument("--per-class", type=int, default=1,
                   help="每类随机抽多少张干净图(默认1)")
    p.add_argument("--force", action="store_true",
                   help="忽略断点续跑, 重新生成已完成的样本")
    p.set_defaults(func=cmd_gen_ref)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
