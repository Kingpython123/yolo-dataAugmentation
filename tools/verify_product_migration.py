# -*- coding: utf-8 -*-
"""迁移后一致性验证。命令退出码非 0 表示有未修复的破损。

逐项检查:
  1. 缺陷库: entry_id 前缀与 class_name 一致, 裁剪图存在, source_image 指向真实文件
  2. analyzed.json: 路径全部指向真实文件(否则增量编目会重复烧 VLM 额度)
  3. 标注: class/reference_class 都是新命名, reference_entry 能在缺陷库里查到,
     generated_image / mask / clean_source 三个路径都存在
  4. 反查链路(regen-rejected 依赖): 用 rejection.scan_rejected_dirs 真跑一遍
  5. 数据集: 两个根目录的类别名都符合 `<产品>_角度N`, 且能被 dataset 正常扫描
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from src.config import load_config          # noqa: E402
from src.dataset import product_of, scan_class_images  # noqa: E402
from src.defect_catalog import load_catalog, load_analyzed  # noqa: E402
from src import rejection                   # noqa: E402

failures = 0

# 迁移前就已存在的问题(经 tools/_diag_migration_failures.py 定性), 作为基线排除。
# 只要实际数量不超过基线, 就不算迁移失败 —— 但仍然打印出来, 因为它们是真问题,
# 只是不属于本次迁移的责任范围。
KNOWN = {
    # 5 条记录的 class 停留在产品级: 干净图来自已删除的 演示素材_四类瓶子褶皱对比
    # 目录, stem 在现存数据集里查不到, 角度不可知 -> 不猜。
    "product_level_class": 5,
    # 56 条 mask 路径指向 outputs/rejected/, 但 rejected 下从来没有 *_mask.png:
    # 流水线对驳回样本只写图不写掩膜, 标注却记了一个从未写出的路径。
    # 这是独立的记账 bug, 与类别改名无关。
    "mask_missing": 56,
    # 15 条 clean_source 指向已删除的 演示素材 目录。
    "clean_source_missing": 15,
    # 反查失败 372 = 256 干净图缺失(类别是一一映射、干净图未动, 迁移前后等价)
    #             + 111 文件名没有类别前缀(临时脚本产出, rejection.py 从不支持)
    #             + 5   产品级前缀 360森呼吸(源图已删, 怎样都反查不了)
    "reverse_lookup_fail": 372,
}


def check(title: str, ok: bool, detail: str = "", known: int = 0,
          actual: int = 0) -> None:
    """known>0 时: 实际数量 <= 基线 -> 记为 KNOWN(不算失败)。"""
    global failures
    if not ok and known and actual <= known:
        print(f"  [KNOWN] {title}  {detail}  (迁移前既有, 基线 {known})")
        return
    mark = "OK  " if ok else "FAIL"
    print(f"  [{mark}] {title}" + (f"  {detail}" if detail else ""))
    if not ok:
        failures += 1


cfg = load_config(None)
print("=" * 92)
print("1) 数据集目录")
print("=" * 92)
for root, name in ((cfg.clean_root(), "干净"), (cfg.defect_root(), "缺陷")):
    imgs = scan_class_images(cfg, root)
    bad = [c for c in imgs if "_角度" not in c]
    check(f"{name}根目录类别命名规范 ({len(imgs)} 个类别)", not bad,
          f"不规范: {bad}" if bad else "")
    prods = sorted({product_of(c) for c in imgs})
    print(f"         产品: {prods}")

print("\n" + "=" * 92)
print("2) 缺陷库")
print("=" * 92)
records = load_catalog(cfg)
bad_prefix = [r["entry_id"] for r in records
              if not str(r["entry_id"]).startswith(str(r["class_name"]) + "__")]
check(f"entry_id 前缀与 class_name 一致 ({len(records)} 条)", not bad_prefix,
      f"不一致 {len(bad_prefix)} 条, 例 {bad_prefix[:2]}" if bad_prefix else "")
bad_cls = [r["entry_id"] for r in records if "_角度" not in str(r["class_name"])]
check("class_name 全部是新命名", not bad_cls,
      f"仍是旧名 {len(bad_cls)} 条" if bad_cls else "")
missing_crop = [r["entry_id"] for r in records
                if not Path(str(r.get("crop_path", ""))).is_file()]
check("裁剪图全部存在", not missing_crop,
      f"缺失 {len(missing_crop)} 个" if missing_crop else "")
missing_src = [r["entry_id"] for r in records
               if r.get("source_image")
               and not Path(str(r["source_image"])).is_file()]
check("source_image 指向真实文件", not missing_src,
      f"失效 {len(missing_src)} 条, 例 {missing_src[:2]}" if missing_src else "")

analyzed = load_analyzed(cfg)
missing_ana = [p for p in analyzed if not Path(p).is_file()]
check(f"analyzed.json 路径有效 ({len(analyzed)} 条)", not missing_ana,
      f"失效 {len(missing_ana)} 条 -> 会重复调用 VLM" if missing_ana else "")

print("\n" + "=" * 92)
print("3) 标注")
print("=" * 92)
entry_ids = {r["entry_id"] for r in records}
ann_files = [REPO / "outputs" / "annotations.jsonl",
             REPO.parent / "测试8.13" / "annotations.jsonl",
             REPO.parent / "测试8.13_batch1" / "annotations.jsonl"]
for ap in ann_files:
    if not ap.is_file():
        continue
    recs = [json.loads(l) for l in
            ap.read_text(encoding="utf-8").splitlines() if l.strip()]
    old_cls = [r for r in recs if "_角度" not in str(r.get("class", ""))]
    bad_ref = [r["reference_entry"] for r in recs
               if r.get("reference_entry") and r["reference_entry"] not in entry_ids]
    miss_gen = [r for r in recs if r.get("generated_image")
                and not Path(str(r["generated_image"])).is_file()]
    miss_mask = [r for r in recs if r.get("mask")
                 and not Path(str(r["mask"])).is_file()]
    miss_clean = [r for r in recs if r.get("clean_source")
                  and not Path(str(r["clean_source"])).is_file()]
    print(f"\n  {ap.parent.name}/{ap.name}  ({len(recs)} 条)")
    check("class 全部是新命名", not old_cls,
          f"{len(old_cls)} 条仍是产品级", KNOWN["product_level_class"], len(old_cls))
    check("reference_entry 能在缺陷库查到", not bad_ref,
          f"查不到 {len(bad_ref)} 条, 例 {bad_ref[:2]}" if bad_ref else "")
    check("generated_image 存在", not miss_gen, f"缺失 {len(miss_gen)}" if miss_gen else "")
    check("mask 存在", not miss_mask, f"缺失 {len(miss_mask)}",
          KNOWN["mask_missing"], len(miss_mask))
    check("clean_source 存在", not miss_clean, f"缺失 {len(miss_clean)}",
          KNOWN["clean_source_missing"], len(miss_clean))
    cp = Counter(r.get("cross_product") for r in recs)
    print(f"         cross_product 分布: {dict(cp)}")

print("\n" + "=" * 92)
print("4) 反查链路 (regen-rejected / gen-augment 方式C 依赖)")
print("=" * 92)
rej = REPO / "outputs" / "rejected"
if rej.is_dir():
    sub = [d for d in rej.iterdir() if d.is_dir()]
    total_ok = total_files = 0
    problems: list[str] = []
    for d in sub:
        rep = rejection.scan_rejected_dirs(cfg, [str(d)], catalog_records=records)
        total_ok += rep.ok_count
        total_files += rep.scanned_files
        problems.extend(rep.problems)
    rate = total_ok / total_files if total_files else 0.0
    check(f"驳回样本反查成功率 {total_ok}/{total_files} = {rate:.1%}",
          rate >= 0.95, f"失败 {len(problems)} 例",
          KNOWN["reverse_lookup_fail"], len(problems))
    cats = Counter()
    for p in problems:
        if "无法从文件名识别出已知类别" in p:
            cats["文件名无类别前缀/产品级前缀"] += 1
        elif "找不到原始干净图" in p:
            cats["干净图不在数据集里"] += 1
        else:
            cats["其他"] += 1
    for k, v in cats.most_common():
        print(f"         {k}: {v}")
else:
    print("  (没有 outputs/rejected, 跳过)")

print("\n" + "=" * 92)
print(f"结论: {'全部通过' if failures == 0 else f'{failures} 项未通过'}")
print("=" * 92)
sys.exit(1 if failures else 0)
