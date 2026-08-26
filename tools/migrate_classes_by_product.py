# -*- coding: utf-8 -*-
"""把样本类别目录按"产品_角度"重命名, 并同步迁移缺陷库/标注/配置。

起因
----
用户人工核对发现: 类别目录名 `1~4.bmp` 在两个根目录下指的是**不同产品** ——
  正样本(有缺陷)/1~4.bmp  = 120蓝瓶 (AQUA-SHINING 120mL 透明瓶, 796x1656)
  负样本(无缺陷)/1~4.bmp  = 60ml多效极润 (不透明白瓶, 675x1370)
而流水线按**同名类别目录**配对 clean_root/<类别> 与 defect_root/<类别>, 且
  cross_class = (ref.class_name != class_name)
只是目录名的字符串比较。于是:
  - 目标 1.bmp(60ml白瓶) + 参考 1.bmp(120蓝瓶) -> 记成同类, 实为跨产品
  - 目标 120蓝瓶 + 参考 1.bmp(也是120蓝瓶)     -> 记成跨类, 实为同产品
实测受影响: 缺陷库 212/838 条(25.3%)产品归属标错; 主产线 1257 条标注里 342 条
涉及标错类别。此外 `120蓝瓶` 因为 defect_root 下没有同名目录, 90 张干净图从未
被用过(类别是按 defect_root 自动扫描的)。

为什么用扁平的 `产品_角度N` 而不是嵌套 `产品/角度N/`
--------------------------------------------------
src/rejection.py 用 `clean_root / class_name / f"{clean_stem}{ext}"` 直接拼路径,
packaging/make_catalog_pack.py 也按 `<root>/<class>/<文件名>` 重建 source_image,
两者都假设"类别目录只有一层"。嵌套会让 regen-rejected 与打包脚本同时失效。
扁平命名按产品名排序后同样自然聚合, 且这两处零改动。

同时保留"角度"作为类别粒度(不合并成产品), 因为:
  - 现有语义不变, allow_cross_class / cross_class_ratio 行为不变
  - 仍可按角度做分层实验
  - 产品级信息通过新增的 data.product_of 映射与标注里的 cross_product 字段表达

用法
----
  python tools/migrate_classes_by_product.py            # 只试算(默认)
  python tools/migrate_classes_by_product.py --apply    # 真正执行
  python tools/migrate_classes_by_product.py --undo <reverse_map.json>
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PROJECT = REPO.parent
CLEAN_ROOT = PROJECT / "实拍负样本（无缺陷）"
DEFECT_ROOT = PROJECT / "实拍正样本（有缺陷）"
IMG_EXTS = {".bmp", ".jpg", ".jpeg", ".png"}

# ---- 类别重命名表(依据用户人工核对 + 分辨率/文件名前缀交叉验证) ----
CLEAN_MAP = {
    "1.jpg": "500多效极润_角度1", "2.jpg": "500多效极润_角度2",
    "3.jpg": "500多效极润_角度3", "4.jpg": "500多效极润_角度4",
    "1.bmp": "60ml多效极润_角度1", "2.bmp": "60ml多效极润_角度2",
    "3.bmp": "60ml多效极润_角度3", "4.bmp": "60ml多效极润_角度4",
    "5.1": "500水次方_角度1", "5.2": "500水次方_角度2",
    "5.3": "500水次方_角度3", "5.4": "500水次方_角度4",
    "120蓝瓶": "120蓝瓶_角度4",   # 全部 90 张文件名前缀都是 "4号"
}
DEFECT_MAP = {
    "1.jpg": "500多效极润_角度1", "2.jpg": "500多效极润_角度2",
    "3.jpg": "500多效极润_角度3", "4.jpg": "500多效极润_角度4",
    "1.bmp": "120蓝瓶_角度1", "2.bmp": "120蓝瓶_角度2",
    "3.bmp": "120蓝瓶_角度3", "4.bmp": "120蓝瓶_角度4",
    "5.1": "500水次方_角度1", "5.2": "500水次方_角度2",
    "5.3": "500水次方_角度3", "5.4": "500水次方_角度4",
}
# 已经是"产品/角度子目录"结构的, 需要把子目录提上来展平
NESTED = {"360森呼吸": {"1": "360森呼吸_角度1", "2": "360森呼吸_角度2",
                        "3": "360森呼吸_角度3", "4": "360森呼吸_角度4"}}

# 预期的文件名前缀, 用作迁移前的自检(防止对照表与磁盘实况不符就动手)
EXPECT_PREFIX = {
    (DEFECT_ROOT, "1.bmp"): "1号", (DEFECT_ROOT, "2.bmp"): "2号",
    (DEFECT_ROOT, "3.bmp"): "3号", (DEFECT_ROOT, "4.bmp"): "4号",
    (CLEAN_ROOT, "120蓝瓶"): "4号",
}


def product_of(cls: str) -> str:
    return cls.split("_角度")[0]


def count_images(d: Path) -> int:
    if not d.is_dir():
        return 0
    return sum(1 for p in d.rglob("*")
               if p.is_file() and p.suffix.lower() in IMG_EXTS)


def plan_dir_renames() -> list[dict]:
    """列出所有目录级别的重命名/搬移动作。"""
    actions: list[dict] = []
    for root, mapping, tag in ((CLEAN_ROOT, CLEAN_MAP, "干净"),
                               (DEFECT_ROOT, DEFECT_MAP, "缺陷")):
        for old, new in mapping.items():
            src, dst = root / old, root / new
            if not src.is_dir():
                continue
            actions.append({"kind": "rename_dir", "tag": tag,
                            "src": str(src), "dst": str(dst),
                            "n_images": count_images(src)})
        for parent, submap in NESTED.items():
            pdir = root / parent
            if not pdir.is_dir():
                continue
            for sub, new in submap.items():
                src, dst = pdir / sub, root / new
                if not src.is_dir():
                    continue
                actions.append({"kind": "rename_dir", "tag": tag,
                                "src": str(src), "dst": str(dst),
                                "n_images": count_images(src)})
            actions.append({"kind": "rmdir_if_empty", "tag": tag,
                            "src": str(pdir), "dst": "", "n_images": 0})
    return actions


def preflight() -> list[str]:
    """迁移前自检。返回问题列表; 非空则不执行。"""
    problems: list[str] = []
    for root in (CLEAN_ROOT, DEFECT_ROOT):
        if not root.is_dir():
            problems.append(f"根目录不存在: {root}")
    if problems:
        return problems

    # 1) 目标名不能已存在
    for root, mapping in ((CLEAN_ROOT, CLEAN_MAP), (DEFECT_ROOT, DEFECT_MAP)):
        for old, new in mapping.items():
            if (root / old).is_dir() and (root / new).exists():
                problems.append(f"目标已存在, 会冲突: {root.name}/{new}")
    for root in (CLEAN_ROOT, DEFECT_ROOT):
        for parent, submap in NESTED.items():
            for sub, new in submap.items():
                if (root / parent / sub).is_dir() and (root / new).exists():
                    problems.append(f"目标已存在, 会冲突: {root.name}/{new}")

    # 2) 磁盘上有但对照表没覆盖的类别 -> 必须先问清楚, 不能默默漏掉
    for root, mapping, tag in ((CLEAN_ROOT, CLEAN_MAP, "干净"),
                               (DEFECT_ROOT, DEFECT_MAP, "缺陷")):
        known = set(mapping) | set(NESTED)
        for d in root.iterdir():
            if d.is_dir() and d.name not in known:
                problems.append(f"[{tag}] 对照表未覆盖的类别目录: {d.name}")

    # 3) 文件名前缀自检: 验证产品归属判断与磁盘实况一致
    for (root, cls), pre in EXPECT_PREFIX.items():
        d = root / cls
        if not d.is_dir():
            continue
        files = [p for p in d.rglob("*")
                 if p.is_file() and p.suffix.lower() in IMG_EXTS]
        bad = [p.name for p in files if not p.name.startswith(pre)]
        if bad:
            problems.append(
                f"[自检失败] {root.name}/{cls} 预期文件名前缀 '{pre}', "
                f"但有 {len(bad)} 个不符, 例: {bad[:3]}")
    return problems


def migrate_catalog(catalog_path: Path, analyzed_path: Path,
                    crops_dir: Path, apply: bool) -> dict:
    """迁移缺陷库: class_name / entry_id / crop_path / source_image + 裁剪图改名。"""
    stats = {"entries": 0, "changed": 0, "crops_renamed": 0,
             "crops_missing": 0, "analyzed_paths": 0, "id_map": {}}
    if not catalog_path.is_file():
        return stats
    records = json.loads(catalog_path.read_text(encoding="utf-8"))
    stats["entries"] = len(records)

    def safe(name: str) -> str:
        return name.replace("（", "(").replace("）", ")").replace("/", "_")

    crop_renames: list[tuple[Path, Path]] = []
    for rec in records:
        old_cls = str(rec.get("class_name", ""))
        new_cls = DEFECT_MAP.get(old_cls)
        if new_cls is None:
            # 360森呼吸 这类嵌套结构的条目, class_name 就是产品名, 需要靠
            # source_image 的子目录判断角度
            if old_cls in NESTED:
                src = str(rec.get("source_image", "")).replace("\\", "/")
                new_cls = None
                for sub, target in NESTED[old_cls].items():
                    if f"/{old_cls}/{sub}/" in src:
                        new_cls = target
                        break
                if new_cls is None:
                    continue
            else:
                continue
        old_eid = str(rec.get("entry_id", ""))
        # entry_id 形如 {class}__{stem}__{i}; 只替换开头的类别段
        if old_eid.startswith(old_cls + "__"):
            new_eid = new_cls + old_eid[len(old_cls):]
        else:
            new_eid = old_eid
        if old_cls == new_cls and old_eid == new_eid:
            continue

        stats["id_map"][old_eid] = new_eid
        rec["class_name"] = new_cls
        rec["entry_id"] = new_eid

        # crop 文件改名
        old_crop = crops_dir / f"{safe(old_eid)}.png"
        new_crop = crops_dir / f"{safe(new_eid)}.png"
        if old_crop.is_file():
            crop_renames.append((old_crop, new_crop))
        else:
            stats["crops_missing"] += 1
        rec["crop_path"] = str(Path(rec.get("crop_path", "")).parent
                               / f"{safe(new_eid)}.png") \
            if rec.get("crop_path") else rec.get("crop_path")

        # source_image 路径里的类别目录段
        si = str(rec.get("source_image", ""))
        if si:
            for sep in ("\\", "/"):
                # 嵌套结构: /360森呼吸/1/ -> /360森呼吸_角度1/
                if old_cls in NESTED:
                    for sub, target in NESTED[old_cls].items():
                        si = si.replace(f"{sep}{old_cls}{sep}{sub}{sep}",
                                        f"{sep}{target}{sep}")
                else:
                    si = si.replace(f"{sep}{old_cls}{sep}",
                                    f"{sep}{new_cls}{sep}")
            rec["source_image"] = si
        stats["changed"] += 1

    stats["crops_renamed"] = len(crop_renames)

    if apply:
        # catalog.json / analyzed.json 目前在 git 里有未提交改动, 所以不能靠
        # git checkout 兜底(那会连未提交的改动一起丢), 单独备份。
        bk = catalog_path.with_suffix(".json.pre_product_migration")
        if not bk.exists():
            shutil.copy2(catalog_path, bk)
        for a, b in crop_renames:
            if b.exists():
                b.unlink()
            a.rename(b)
        catalog_path.write_text(
            json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")

    # analyzed.json: 里面是已分析过的源图绝对路径, 不迁移会导致增量编目
    # 把 838 张图全当成新图重新分析(= 838 次 VLM 调用)
    if analyzed_path.is_file():
        data = json.loads(analyzed_path.read_text(encoding="utf-8"))
        paths = data if isinstance(data, list) else data.get("analyzed", [])
        new_paths, n = [], 0
        for p in paths:
            s = str(p)
            orig = s
            for old_cls, new_cls in DEFECT_MAP.items():
                for sep in ("\\", "/"):
                    s = s.replace(f"{sep}{old_cls}{sep}", f"{sep}{new_cls}{sep}")
            for parent, submap in NESTED.items():
                for sub, target in submap.items():
                    for sep in ("\\", "/"):
                        s = s.replace(f"{sep}{parent}{sep}{sub}{sep}",
                                      f"{sep}{target}{sep}")
            if s != orig:
                n += 1
            new_paths.append(s)
        stats["analyzed_paths"] = n
        if apply:
            bk = analyzed_path.with_suffix(".json.pre_product_migration")
            if not bk.exists():
                shutil.copy2(analyzed_path, bk)
            out = new_paths if isinstance(data, list) else {**data,
                                                            "analyzed": new_paths}
            analyzed_path.write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return stats


def migrate_annotations(path: Path, id_map: dict, apply: bool) -> dict:
    """迁移标注: class / reference_class / reference_entry + 各绝对路径。"""
    stats = {"records": 0, "changed": 0}
    if not path.is_file():
        return stats
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out_lines: list[str] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            out_lines.append(line)
            continue
        stats["records"] += 1
        before = json.dumps(rec, ensure_ascii=False, sort_keys=True)

        # 目标类别用 CLEAN_MAP(它来自 clean_root), 参考类别用 DEFECT_MAP
        if rec.get("class") in CLEAN_MAP:
            rec["class"] = CLEAN_MAP[rec["class"]]
        if rec.get("reference_class") in DEFECT_MAP:
            rec["reference_class"] = DEFECT_MAP[rec["reference_class"]]
        if rec.get("reference_entry") in id_map:
            rec["reference_entry"] = id_map[rec["reference_entry"]]

        # 绝对路径: clean_source 走 CLEAN_MAP, 其余(产出/掩膜)走类别目录名
        for key, mapping in (("clean_source", CLEAN_MAP),
                             ("generated_image", CLEAN_MAP),
                             ("mask", CLEAN_MAP)):
            v = rec.get(key)
            if not v:
                continue
            s = str(v)
            for old, new in mapping.items():
                for sep in ("\\", "/"):
                    s = s.replace(f"{sep}{old}{sep}", f"{sep}{new}{sep}")
            rec[key] = s

        # cross_product: 依据产品前缀重算(这是本次修复的核心字段)
        cls, rcls = str(rec.get("class", "")), str(rec.get("reference_class", ""))
        if cls and rcls:
            rec["cross_product"] = product_of(cls) != product_of(rcls)

        after = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        if before != after:
            stats["changed"] += 1
        out_lines.append(json.dumps(rec, ensure_ascii=False))

    if apply and stats["changed"]:
        backup = path.with_suffix(path.suffix + ".pre_product_migration")
        if not backup.exists():
            shutil.copy2(path, backup)
        path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return stats


def plan_output_dir_renames(outputs: Path) -> list[dict]:
    """产出目录下的按类别子目录(generated/masks/rejected/debug)一并改名。"""
    actions: list[dict] = []
    for sub in ("generated", "masks", "rejected", "debug"):
        base = outputs / sub
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            # 产出目录是按 clean_root 的类别建的
            new = CLEAN_MAP.get(d.name)
            if new is None and d.name in NESTED:
                continue   # 嵌套产物少见, 跳过并在报告里提示
            if new and new != d.name:
                actions.append({"kind": "rename_dir", "tag": f"outputs/{sub}",
                                "src": str(d), "dst": str(base / new),
                                "n_images": count_images(d)})
    return actions


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正执行(默认只试算)")
    ap.add_argument("--undo", default=None, help="用反向映射文件撤销")
    args = ap.parse_args()

    if args.undo:
        rev = json.loads(Path(args.undo).read_text(encoding="utf-8"))
        print(f"撤销 {len(rev['dir_renames'])} 个目录改名...")
        for a in reversed(rev["dir_renames"]):
            dst, src = Path(a["dst"]), Path(a["src"])
            if dst.is_dir():
                src.parent.mkdir(parents=True, exist_ok=True)
                dst.rename(src)
                print(f"  {dst.name} -> {src.relative_to(src.parent.parent)}")
        print("目录已还原。缺陷库/标注请用 .pre_product_migration 备份或 git 恢复。")
        return 0

    print("=" * 96)
    print("迁移前自检")
    print("=" * 96)
    problems = preflight()
    if problems:
        for p in problems:
            print(f"  [问题] {p}")
        print("\n自检未通过, 不执行任何修改。")
        return 1
    print("  全部通过: 目标名无冲突, 无未覆盖类别, 文件名前缀与产品判断一致")

    dir_actions = plan_dir_renames()
    outputs = REPO / "outputs"
    out_actions = plan_output_dir_renames(outputs)

    print("\n" + "=" * 96)
    print(f"步骤1: 样本目录重命名 ({len(dir_actions)} 项)")
    print("=" * 96)
    print(f"{'来源':<10s}{'旧名':<16s}{'新名':<22s}{'图片数':>7s}")
    print("-" * 96)
    total_imgs = 0
    for a in dir_actions:
        if a["kind"] != "rename_dir":
            continue
        src, dst = Path(a["src"]), Path(a["dst"])
        old_disp = (f"{src.parent.name}/{src.name}"
                    if src.parent.name not in (CLEAN_ROOT.name, DEFECT_ROOT.name)
                    else src.name)
        print(f"{a['tag']:<10s}{old_disp:<16s}{dst.name:<22s}{a['n_images']:>7d}")
        total_imgs += a["n_images"]
    print(f"\n  合计 {total_imgs} 张图片, 全部是**目录级重命名**(不逐个搬文件, 瞬间完成)")

    print("\n" + "=" * 96)
    print("步骤2: 缺陷库迁移(试算)")
    print("=" * 96)
    cat = outputs / "catalog" / "catalog.json"
    ana = outputs / "catalog" / "analyzed.json"
    crops = outputs / "catalog" / "crops"
    st = migrate_catalog(cat, ana, crops, apply=False)
    print(f"  条目总数 {st['entries']}, 需改动 {st['changed']}")
    print(f"  entry_id 改名 {len(st['id_map'])} 条")
    print(f"  裁剪图改名 {st['crops_renamed']} 个, 缺失 {st['crops_missing']} 个")
    print(f"  analyzed.json 路径改写 {st['analyzed_paths']} 条 "
          f"(不改会导致重新分析 -> {st['analyzed_paths']} 次 VLM 调用)")
    sample = list(st["id_map"].items())[:4]
    for a, b in sample:
        print(f"    例: {a}\n     -> {b}")

    print("\n" + "=" * 96)
    print("步骤3: 标注迁移(试算)")
    print("=" * 96)
    ann_paths = [outputs / "annotations.jsonl",
                 PROJECT / "测试8.13" / "annotations.jsonl",
                 PROJECT / "测试8.13_batch1" / "annotations.jsonl"]
    for p in ann_paths:
        s = migrate_annotations(p, st["id_map"], apply=False)
        if s["records"]:
            print(f"  {p.relative_to(PROJECT)}: {s['records']} 条, "
                  f"需改动 {s['changed']}")

    print("\n" + "=" * 96)
    print(f"步骤4: 产出目录重命名 ({len(out_actions)} 项)")
    print("=" * 96)
    for a in out_actions:
        print(f"  {a['tag']:<18s}{Path(a['src']).name:<12s} -> "
              f"{Path(a['dst']).name:<22s}({a['n_images']} 个文件)")

    if not args.apply:
        print("\n" + "=" * 96)
        print("以上为试算结果, 未做任何修改。加 --apply 执行。")
        print("=" * 96)
        return 0

    # ---------------- 执行 ----------------
    print("\n" + "=" * 96)
    print("开始执行")
    print("=" * 96)
    done: list[dict] = []
    for a in dir_actions + out_actions:
        src, dst = Path(a["src"]), Path(a["dst"])
        if a["kind"] == "rmdir_if_empty":
            if src.is_dir() and not any(src.iterdir()):
                src.rmdir()
                print(f"  删除空目录 {src.name}")
            continue
        if not src.is_dir():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dst)
        done.append(a)
        print(f"  {src.name} -> {dst.name}")

    st = migrate_catalog(cat, ana, crops, apply=True)
    print(f"  缺陷库: {st['changed']} 条改动, {st['crops_renamed']} 个裁剪图改名")
    for p in ann_paths:
        s = migrate_annotations(p, st["id_map"], apply=True)
        if s["changed"]:
            print(f"  标注 {p.name}: {s['changed']} 条改动(已备份 "
                  f".pre_product_migration)")

    rev = {"dir_renames": done, "id_map": st["id_map"]}
    rev_path = REPO / "outputs" / "product_migration_reverse_map.json"
    rev_path.write_text(json.dumps(rev, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    print(f"\n反向映射已写入 {rev_path}")
    print("如需撤销目录改名: "
          f"python tools/migrate_classes_by_product.py --undo {rev_path.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
