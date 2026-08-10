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

from app.runtime import bootstrap
from src.config import load_config

DEFAULT_REJECTED_DIR = '没打标签的'


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
    """离线体检缺陷库: 格式/类型/严重度/字段完整度/裁剪图是否存在。

    判定逻辑已提炼到 src/catalog_report.py, 图形界面复用同一份结果;
    这里只负责把它渲染成控制台文本。
    """
    from src import catalog_report
    cfg = load_config(args.config)
    report = catalog_report.build_report(cfg)
    for line in catalog_report.format_lines(report):
        print(line)


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


def cmd_fail_summary(args):
    """离线统计 outputs/fail_log.jsonl 里的失败原因分类(不调用API)。"""
    import json
    from collections import Counter

    cfg = load_config(args.config)
    log_path = cfg.resolve(cfg.output.get("root", "outputs")) / "fail_log.jsonl"
    if not log_path.exists():
        print(f"没有失败日志: {log_path} (说明还没跑过, 或者这次没有失败)")
        return

    recs = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            recs.append(json.loads(line))
        except json.JSONDecodeError:
            continue

    if args.classes:
        only = set(c.strip() for c in args.classes.split(","))
        recs = [r for r in recs if r.get("class") in only]
    if args.since:
        recs = [r for r in recs if r.get("time", "") >= args.since]

    print(f"失败日志: {log_path}")
    print(f"记录总数: {len(recs)}")
    if not recs:
        return

    by_reason = Counter(r["reason"] for r in recs)
    print("\n按原因分类:")
    label = {
        "channel_unavailable": "渠道不可用(中转站没货, 重试无意义, 需等中转站恢复)",
        "rate_limited": "限流 429(等待后重试可能有效)",
        "service_unavailable": "服务不可用 503",
        "server_error": "服务端 5xx 错误",
        "timeout": "请求超时",
        "bad_response": "响应无法解析/未返回图像",
        "image_open_failed": "干净图打开失败(文件损坏或路径问题)",
        "synthesis_exception": "合成流程内部异常(需看 detail 排查代码问题)",
        "other": "其它/未分类",
        "exhausted_retries": "任务三次重试后仍失败(汇总记录, 具体原因看同 stem 的其它条目)",
    }
    for reason, n in by_reason.most_common():
        print(f"  {n:>4}  {reason:<22} {label.get(reason, '')}")

    print("\n按类别分类:")
    by_class = Counter(r.get("class", "?") for r in recs)
    for cls, n in sorted(by_class.items()):
        print(f"  {cls:<8} {n:>4}")

    # 每个 stem 唯一失败(去重后的"真实失败任务数", 因为同一任务重试多次会有多条)
    stems = {r["stem"] for r in recs if r.get("stem")}
    print(f"\n涉及的不同任务(stem)数: {len(stems)}")
    print(f"(若担心是否还在发生, 可直接 `Get-Content {log_path.name} -Wait -Tail 5`"
          f" 实时观察正在跑的进程)")


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
            print(f"\n预计 API 调用: 约 {len(tasks) * 2}~{len(tasks) * 4} 次"
                 f"(每张编辑+质检至少各一次, 视重试次数)")
            print(f"按单张 50~60 秒、并发 {workers} 估算, "
                 f"预计耗时 {len(tasks) * 50 / workers / 60:.0f}~"
                 f"{len(tasks) * 60 / workers / 60:.0f} 分钟")
            print("\n清单(前 10 条):")
            for t in tasks[:10]:
                print(f"  {t.stem}")
                print(f"    干净图={t.clean_path.name}  参考={t.forced_ref['entry_id']}")
        print("\n(试算, 未调用任何 API。确认无误后去掉 --dry-run 即开始)")
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
    print(f"\n已归档 {archived} 个原驳回文件到各目录下的 已重生成/ 子目录")
    return 0



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

    print(f"\n形态最相似的 {len(hits)} 条(距离越小越像):")
    for i, h in enumerate(hits, 1):
        r = h.record
        print(f"{i:>3}. d={h.distance:.3f}  {h.entry_id}")
        print(f"      类型={r.get('defect_type')} 严重度={r.get('severity')} "
              f"条数={r.get('count')} 走向={str(r.get('orientation',''))[:40]}")

    print("\n可直接把这些 entry_id 交给 gen-augment 做定向补充, 例如:")
    ids_preview = " ".join(h.entry_id for h in hits[:3])
    print(f"  run.py gen-augment --classes 1.jpg --per-ref 2 --refs {ids_preview}")
    print("或者一步到位(gen-augment 内置同样的检索):")
    seed_arg = (f"--similar-to-image {args.image}" if args.image
                else f"--similar-to-entry {args.entry}")
    print(f"  run.py gen-augment --classes 1.jpg --per-ref 2 "
          f"{seed_arg} --similar-top-k {args.top_k}")
    return 0


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

    # ---- 方式 B: 按形态相似检索 ----
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
        print("  --similar-to-image <图>     按形态相似检索(需要一张出问题的图)")
        print("  --similar-to-entry <id>     以缺陷库某条目为种子按形态相似检索")
        print("  --severity 4-5 等属性条件   按形态属性筛选")
        print("  --refs <entry_id> ...      直接指定参考条目")
        return 2

    if args.max_refs and len(selected) > args.max_refs:
        print(f"\n[限制] 选中 {len(selected)} 条, 按 --max-refs 截断为前 "
              f"{args.max_refs} 条")
        selected = selected[:args.max_refs]

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    print(f"\n最终选中参考条目 {len(selected)} 条, 目标类别 {classes}, "
          f"每条每类 {args.per_ref} 张")
    print("\n参考条目清单(前 15 条):")
    for eid in selected[:15]:
        r = by_entry[eid]
        print(f"  {eid}")
        print(f"    类型={r.get('defect_type')} 严重度={r.get('severity')} "
              f"条数={r.get('count')} 走向={r.get('orientation','')[:24]}")

    tasks = plan_augment_by_references(cfg, selected, classes,
                                      per_ref=args.per_ref,
                                      catalog_records=records)
    workers = max(1, int(cfg.api.get("max_workers", 3)))
    print(f"\n将生成 {len(tasks)} 张样本(落进 outputs/generated/, "
          f"质检不合格的照常进 outputs/rejected/)")
    print(f"预计 API 调用约 {len(tasks) * 2}~{len(tasks) * 4} 次"
          f"(每张编辑+质检至少各一次, 视重试次数)")
    print(f"按单张 50~60 秒、并发 {workers} 估算, 预计耗时 "
          f"{len(tasks) * 50 / workers / 60:.0f}~"
          f"{len(tasks) * 60 / workers / 60:.0f} 分钟")

    if not args.yes:
        print("\n(以上为预览, 未调用任何 API。确认无误后加 --yes 开始生成)")
        return 0

    augment_by_references(cfg, selected, classes, per_ref=args.per_ref,
                          resume=not args.force)
    return 0



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

def main():
    ap = argparse.ArgumentParser(description="工业瓶子缺陷合成流水线")
    ap.add_argument("--config", default=None, help="config.yaml 路径")
    ap.add_argument("--workspace", default=None,
                    help="工作区目录(存放 config.yaml/outputs/logs/jobs);"
                         "默认取用户配置, 源码运行时回退为仓库根目录")
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

    p = sub.add_parser(
        "fail-summary",
        help="离线统计 outputs/fail_log.jsonl 里的失败原因分类(不调用API)")
    p.add_argument("--classes", type=str, default=None, help="仅统计指定类别")
    p.add_argument("--since", type=str, default=None,
                   help="仅统计此时间之后的记录, ISO格式如 2026-08-04T15:00:00")
    p.set_defaults(func=cmd_fail_summary)

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
    p.add_argument("--similar-to-image", default=None,
                   help="[方式B] 以一张出问题的图为种子, 检索形态相似的参考条目")
    p.add_argument("--similar-to-entry", default=None,
                   help="[方式B] 以缺陷库某条目为种子, 检索形态相似的参考条目")
    p.add_argument("--similar-top-k", type=int, default=10,
                   help="[方式B] 相似检索取前 N 条(默认10)")
    p.add_argument("--refs", nargs="+", default=None,
                   help="直接指定参考条目 entry_id, 可多个")
    p.add_argument("--max-refs", type=int, default=None,
                   help="选中过多时只取前 N 条(控制成本)")
    p.add_argument("--yes", action="store_true",
                   help="确认执行(不加则只预览清单与成本, 不调用 API)")
    p.add_argument("--force", action="store_true", help="忽略断点续跑")
    p.set_defaults(func=cmd_gen_augment)

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

    args = ap.parse_args()
    # 装配运行环境: 固定标准流编码、把默认 config.yaml 指向工作区、
    # 注入凭据管理器里的 API key。源码方式运行且未设置工作区时,
    # 工作区回退为仓库根目录, 行为与改造前一致。
    bootstrap.install(workspace=getattr(args, "workspace", None))
    # 子命令可以返回退出码; 返回 None 视为成功, 保持原有行为
    return args.func(args) or 0


if __name__ == "__main__":
    raise SystemExit(main())
