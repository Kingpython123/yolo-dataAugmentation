"""阶段三门槛验证: 形态描述子的检索质量。

必须两道门槛都过才允许接入 gen-augment 的方式 B。不通过就报告问题、保持方式
B 不可用, 退路是继续用方式 A(按属性筛)/方式 C(按产出图反查)。

门槛一 自检
  拿缺陷库里某条目自己的裁剪图当查询, 它必须排第 1。不成立说明描述子计算或
  检索链路有 bug(而不是"效果不够好")。

门槛二 属性一致性
  检索结果的属性分布必须显著偏向查询条目的取值, 而不是接近全库随机分布。
  这里用三个可量化的指标:
    - orientation 关键词命中率: top-k 里与查询条目共享主走向关键词的比例,
      须显著高于全库基线
    - defect_type 一致率: 划痕查划痕、变形查变形的比例须高于全库基线
    - count 的中位绝对偏差: top-k 与查询条目的条数差, 须小于随机取样的期望差

为什么用"高于基线"而不是绝对阈值: 缺陷库本身分布极不均衡(变形 622 / 划痕 46,
含"斜向"的 511/668), 定绝对阈值会把"其实什么都没学到"误判为通过 —— 随便返回
一堆变形+斜向的条目就能轻松达到 70% 命中率。必须跟基线比。
"""
from __future__ import annotations

import json
import random
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np  # noqa: E402

from src import morphology  # noqa: E402
from src.config import load_config  # noqa: E402

OUT_FILE = REPO / "tmp_morphology_result.txt"

# 自检抽样条数(全量 674 张两两比较代价高, 抽样足以暴露链路 bug)
SELFTEST_SAMPLE = 25

# 一致性检验抽样条数与 top-k
CONSISTENCY_SAMPLE = 40
TOP_K = 10

# 走向关键词: 与 catalog_query.ORIENTATION_KEYWORDS 保持一致的判定口径
ORIENT_KEYWORDS = ("斜向", "横向", "竖向", "纵向", "交叉", "多向", "放射")

SEED = 20260810


def orient_keys(rec: dict) -> set:
    text = str(rec.get("orientation", "") or "")
    return {k for k in ORIENT_KEYWORDS if k in text}


def main() -> int:
    lines: list[str] = []
    problems: list[str] = []

    cfg = load_config()
    catalog_path = cfg.out_path("catalog") / "catalog.json"
    records = json.loads(catalog_path.read_text(encoding="utf-8"))
    by_id = {r["entry_id"]: r for r in records}

    lines.append(f"缺陷库条目: {len(records)}")

    # ---------- 构建描述子 ----------
    t0 = time.time()
    descs = morphology.get_descriptors(cfg, records, rebuild=True,
                                       progress=False)
    build_s = time.time() - t0
    lines.append(f"描述子构建: {len(descs)} 条, 耗时 {build_s:.1f} 秒")
    lines.append(f"缓存文件: {morphology.cache_path(cfg)}")

    if len(descs) < len(records) * 0.95:
        problems.append(f"描述子只覆盖 {len(descs)}/{len(records)} 条, "
                        f"裁剪图可能大量缺失")

    # 缓存可复用性: 立刻重新 load 一次, 应命中而不是重建
    t0 = time.time()
    cached = morphology.load_cache(cfg)
    load_s = time.time() - t0
    if cached is None:
        problems.append("刚写入的缓存立刻读取失败(meta 校验或写入有问题)")
    else:
        lines.append(f"缓存读取: {len(cached)} 条, 耗时 {load_s:.2f} 秒")
        # 抽一条比对数值, 确认缓存往返无损
        sample_id = next(iter(descs))
        a, b = descs[sample_id], cached[sample_id]
        if not np.allclose(a.concat(), b.concat(), atol=1e-6):
            problems.append("缓存往返后描述子数值不一致")

    lines.append("")

    # ---------- 门槛一: 自检 ----------
    rng = random.Random(SEED)
    ids_with_desc = sorted(descs.keys())
    sample_ids = rng.sample(ids_with_desc,
                            min(SELFTEST_SAMPLE, len(ids_with_desc)))

    lines.append("=== 门槛一: 自检(自己查自己应排第1) ===")
    rank1 = 0
    failures: list[str] = []
    for eid in sample_ids:
        crop = cfg.resolve(str(by_id[eid].get("crop_path", "")))
        hits = morphology.query_by_image(cfg, crop, records, top_k=1)
        if hits and hits[0].entry_id == eid:
            rank1 += 1
        else:
            got = hits[0].entry_id if hits else "(空)"
            failures.append(f"{eid} -> 第1名是 {got}")
    lines.append(f"抽样 {len(sample_ids)} 条, 自己排第1: {rank1}")
    for f in failures[:5]:
        lines.append(f"  未通过: {f}")
    if rank1 != len(sample_ids):
        problems.append(f"门槛一未通过: {rank1}/{len(sample_ids)} 条自己排第1"
                        f"(应为全部)")
    lines.append("")

    # ---------- 门槛二: 属性一致性 ----------
    lines.append(f"=== 门槛二: 属性一致性(top-{TOP_K} vs 全库基线) ===")

    # 全库基线
    all_types = [r.get("defect_type") for r in records]
    type_base = {t: all_types.count(t) / len(all_types) for t in set(all_types)}
    all_counts = [int(r.get("count", 1) or 1) for r in records]

    sample2 = rng.sample(ids_with_desc,
                         min(CONSISTENCY_SAMPLE, len(ids_with_desc)))

    orient_hit_rates: list[float] = []
    orient_baselines: list[float] = []
    type_hit_rates: list[float] = []
    type_baselines: list[float] = []
    count_devs: list[float] = []
    count_rand_devs: list[float] = []

    for eid in sample2:
        q = by_id[eid]
        hits = morphology.query_by_entry(cfg, eid, records, top_k=TOP_K)
        if not hits:
            continue
        hit_recs = [h.record for h in hits if h.record]

        # orientation 关键词命中率
        qk = orient_keys(q)
        if qk:
            hit = sum(1 for r in hit_recs if orient_keys(r) & qk)
            orient_hit_rates.append(hit / len(hit_recs))
            base = sum(1 for r in records
                       if r["entry_id"] != eid and orient_keys(r) & qk)
            orient_baselines.append(base / max(1, len(records) - 1))

        # defect_type 一致率
        qt = q.get("defect_type")
        same = sum(1 for r in hit_recs if r.get("defect_type") == qt)
        type_hit_rates.append(same / len(hit_recs))
        type_baselines.append(type_base.get(qt, 0.0))

        # count 偏差
        qc = int(q.get("count", 1) or 1)
        devs = [abs(int(r.get("count", 1) or 1) - qc) for r in hit_recs]
        count_devs.append(statistics.median(devs))
        rand_sample = rng.sample(all_counts, min(TOP_K, len(all_counts)))
        count_rand_devs.append(
            statistics.median([abs(c - qc) for c in rand_sample]))

    def mean(xs: list[float]) -> float:
        return sum(xs) / len(xs) if xs else 0.0

    o_hit, o_base = mean(orient_hit_rates), mean(orient_baselines)
    t_hit, t_base = mean(type_hit_rates), mean(type_baselines)
    c_dev, c_rand = mean(count_devs), mean(count_rand_devs)

    lines.append(f"抽样 {len(sample2)} 条种子, 每条取 top-{TOP_K}")
    lines.append(f"  走向关键词命中率: {o_hit:.3f}  (全库基线 {o_base:.3f})")
    lines.append(f"  缺陷类型一致率:   {t_hit:.3f}  (全库基线 {t_base:.3f})")
    lines.append(f"  条数中位偏差:     {c_dev:.2f}  (随机取样 {c_rand:.2f}, 越小越好)")

    # 判定: 要求每项都比基线好, 且走向/类型至少有一项明显好(相对提升 >= 8%)
    if o_hit <= o_base:
        problems.append(f"走向命中率 {o_hit:.3f} 未超过基线 {o_base:.3f}")
    if t_hit <= t_base:
        problems.append(f"类型一致率 {t_hit:.3f} 未超过基线 {t_base:.3f}")
    if c_dev >= c_rand:
        problems.append(f"条数偏差 {c_dev:.2f} 未优于随机 {c_rand:.2f}")

    rel_o = (o_hit - o_base) / o_base if o_base > 0 else 0.0
    rel_t = (t_hit - t_base) / t_base if t_base > 0 else 0.0
    lines.append(f"  相对提升: 走向 {rel_o * 100:+.1f}%, 类型 {rel_t * 100:+.1f}%")
    if max(rel_o, rel_t) < 0.08:
        problems.append(
            f"走向/类型的相对提升均不足 8%(走向 {rel_o * 100:+.1f}%, "
            f"类型 {rel_t * 100:+.1f}%), 检索区分度不够, 不宜接入生成")

    lines.append("")

    # ---------- 定性样例: 供人工扫一眼 ----------
    lines.append("=== 定性样例(人工核对用) ===")
    # 挑一条划痕和一条大面积变形, 这两类形态差异最直观
    scratch = next((r for r in records
                    if r.get("defect_type") == "划痕"
                    and r["entry_id"] in descs), None)
    deform = next((r for r in records
                   if r.get("defect_type") == "变形"
                   and int(r.get("severity", 3) or 3) >= 4
                   and r["entry_id"] in descs), None)
    for seed_rec in (scratch, deform):
        if seed_rec is None:
            continue
        lines.append(f"\n种子: {seed_rec['entry_id']}")
        lines.append(f"  类型={seed_rec.get('defect_type')} "
                     f"严重度={seed_rec.get('severity')} "
                     f"条数={seed_rec.get('count')} "
                     f"走向={str(seed_rec.get('orientation',''))[:30]}")
        hits = morphology.query_by_entry(cfg, seed_rec["entry_id"], records,
                                        top_k=5)
        for h in hits:
            r = h.record
            lines.append(f"  d={h.distance:.3f}  {h.entry_id}")
            lines.append(f"      类型={r.get('defect_type')} "
                         f"严重度={r.get('severity')} "
                         f"条数={r.get('count')} "
                         f"走向={str(r.get('orientation',''))[:30]}")

    lines.append("")
    lines.append("PASS" if not problems else "FAIL:\n" + "\n".join(
        f"  - {p}" for p in problems))
    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
