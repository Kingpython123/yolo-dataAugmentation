"""度量"生成褶皱的走向分布 vs 参考褶皱的走向分布"(离线, 零 API)。

为什么需要这个工具:
  VLM 的 shape_fidelity 曾被 QC prompt 里"仅记录、非主要门槛"这句话压扁在
  7~8 分区间, 45% 的样本文字明确写着走向不符、其中 78% 仍给 >=7 分, 分数与
  自己的文字自相矛盾, 不能单独作为判据。
  而参考浮雕图(0b_structure)与生成的明暗场(3b_shading_field)都是"只含几何、
  不含颜色与印刷内容"的表示, 可以直接做客观配对比较。

关键在于比较方式: 要做"参考 vs 生成"的配对比较, 而不是"用指标去预测质检
标签"。后者试过, 完全没有区分能力(两组差异仅 0.022); 前者能清楚看出模型
系统性地把褶皱画得比参考更平行、更规则。

指标(方向按幅值加权, 角度模 180 度):
  ent : 方向熵。越大 = 走向越多样(多向交叉); 越小 = 越单一。
  dom : 主方向占比 / 均匀分布占比。越大 = 越集中在单一走向(越"等距平行")。
  ent_delta / dom_ratio: 生成相对参考的变化, 是真正要看的量。
    dom_ratio > 1 且 ent_delta < 0  -> 生成比参考更平行更规则(最常见的失败)
    dom_ratio < 1 且 ent_delta > 0  -> 生成比参考更散(参考本身很平行时会这样)

用法:
  python tools/check_orientation_match.py <debug目录或其父目录> [--csv 输出.csv]
  例: python tools/check_orientation_match.py outputs/debug/1.jpg
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

NBINS = 18   # 每 10 度一个 bin


def orient_hist(img: np.ndarray, nbins: int = NBINS,
                mag_pct: float = 80.0) -> np.ndarray | None:
    """梯度方向直方图, 按幅值加权, 只统计显著梯度(避免平坦区噪声主导)。"""
    g = cv2.GaussianBlur(img.astype(np.float32), (0, 0), 1.5)
    gx = cv2.Sobel(g, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(g, cv2.CV_32F, 0, 1, ksize=3)
    mag = np.hypot(gx, gy)
    ang = np.rad2deg(np.arctan2(gy, gx)) % 180.0
    thr = np.percentile(mag, mag_pct)
    sel = mag > max(thr, 1e-6)
    if sel.sum() < 50:
        return None
    h, _ = np.histogram(ang[sel], bins=nbins, range=(0, 180), weights=mag[sel])
    s = h.sum()
    return h / s if s > 0 else None


def entropy(h: np.ndarray) -> float:
    p = h[h > 0]
    return float(-(p * np.log(p)).sum()) if p.size else 0.0


def dominance(h: np.ndarray) -> float:
    """主方向占比相对均匀分布的倍数。1.0=完全均匀, 越大越集中。"""
    return float(h.max() * len(h))


def analyse(task_dir: Path) -> list[dict]:
    out = []
    for struct_f in sorted(task_dir.glob("a*_0b_structure.png")):
        tag = struct_f.name.split("_")[0]
        field_f = task_dir / f"{tag}_3b_shading_field.png"
        if not field_f.exists():
            continue
        ref = np.array(Image.open(struct_f).convert("L"))
        gen = np.array(Image.open(field_f).convert("L"))
        h_ref, h_gen = orient_hist(ref), orient_hist(gen)
        if h_ref is None or h_gen is None:
            continue
        e_ref, e_gen = entropy(h_ref), entropy(h_gen)
        d_ref, d_gen = dominance(h_ref), dominance(h_gen)
        out.append({
            "task": task_dir.name, "attempt": tag,
            "ent_ref": e_ref, "ent_gen": e_gen, "ent_delta": e_gen - e_ref,
            "dom_ref": d_ref, "dom_gen": d_gen,
            "dom_ratio": d_gen / max(d_ref, 1e-9),
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("path", help="debug 目录(含 a*_0b_structure.png 的任务目录, 或其父目录)")
    ap.add_argument("--csv", default=None, help="把明细写入 csv")
    args = ap.parse_args()

    root = Path(args.path)
    if not root.exists():
        print(f"路径不存在: {root}")
        return 1

    dirs = ([root] if list(root.glob("a*_0b_structure.png"))
            else [p for p in root.iterdir() if p.is_dir()])
    rows: list[dict] = []
    for d in dirs:
        rows.extend(analyse(d))

    if not rows:
        print("没有找到可比对的样本(需要 a*_0b_structure.png 与 a*_3b_shading_field.png 成对存在)")
        return 1

    print(f"样本数: {len(rows)}\n")
    print(f"{'ent参考':>8s} {'ent生成':>8s} {'ent差':>7s}  "
          f"{'dom参考':>8s} {'dom生成':>8s} {'dom倍数':>8s}  任务")
    print("-" * 88)
    for r in sorted(rows, key=lambda x: -x["dom_ratio"]):
        print(f"{r['ent_ref']:8.3f} {r['ent_gen']:8.3f} {r['ent_delta']:+7.3f}  "
              f"{r['dom_ref']:8.2f} {r['dom_gen']:8.2f} {r['dom_ratio']:8.2f}  "
              f"{r['task'][:34]}")

    ed = np.array([r["ent_delta"] for r in rows])
    dr = np.array([r["dom_ratio"] for r in rows])
    more_parallel = int(((dr > 1.15) & (ed < 0)).sum())
    more_diffuse = int(((dr < 0.87) & (ed > 0)).sum())
    print()
    print(f"方向熵变化 均值: {ed.mean():+.3f}   (负=生成比参考更单一)")
    print(f"集中度倍数 均值: {dr.mean():.2f}    (>1=生成比参考更平行)")
    print(f"明显更平行更规则的样本: {more_parallel}/{len(rows)} "
          f"({more_parallel/len(rows)*100:.0f}%)")
    print(f"明显更散的样本:         {more_diffuse}/{len(rows)} "
          f"({more_diffuse/len(rows)*100:.0f}%)")
    print(f"走向分布基本吻合的样本: {len(rows)-more_parallel-more_diffuse}/{len(rows)} "
          f"({(len(rows)-more_parallel-more_diffuse)/len(rows)*100:.0f}%)")

    if args.csv:
        import csv
        with open(args.csv, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
            w.writeheader()
            w.writerows(rows)
        print(f"\n明细已写入 {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
