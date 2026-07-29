"""按当前配置离线重判已生成样本(零 API 成本)。

动机
----
质检阈值是面向 YOLO 训练标定的, 会反复调整。每次调完阈值都重新生成一遍
太贵, 而分数其实已经存在 annotations.jsonl 里 —— 直接用存下来的各维度分数
按新配置重算即可, 把之前被误驳回的样本捞回训练集, 也把不该收的踢出去。

判定规则复用 quality_check.judge(), 与在线质检完全一致, 不存在两套标准。

可复判的前提: 记录里存有全部 6 个维度的分数。旧格式记录(缺
highlight_natural / seam_continuity)与质检未跑成的记录无法离线复判, 会被
如实列为"不可复判", 需要重跑或后续的在线补判。

产物变更(仅在 --apply 时):
  rejected -> 合格: 图挪到 outputs/generated/<类>/, 掩膜挪到 outputs/masks/<类>/
  合格 -> rejected: 反向挪回 outputs/rejected/<类>/
  annotations.jsonl 原地重写(先备份 annotations.jsonl.bak)
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import quality_check
from .config import Config
from .dataset import safe_name


@dataclass
class Verdict:
    stem: str
    class_name: str
    was_accepted: bool
    now_accepted: bool
    realism_old: float
    realism_new: float
    fails: list
    record: dict
    decidable: bool = True
    note: str = ""

    @property
    def changed(self) -> bool:
        return self.decidable and self.was_accepted != self.now_accepted


def _read_records(ann_path: Path) -> list[dict]:
    if not ann_path.exists():
        return []
    out = []
    for line in ann_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _dedupe(records: list[dict]) -> tuple[list[dict], int]:
    """同一 stem 可能因 --force 重跑写入多条, 只保留最后一条(最新结果)。"""
    latest: dict[str, dict] = {}
    for r in records:
        gi = r.get("generated_image")
        if not gi:
            continue
        latest[Path(gi).stem] = r
    return list(latest.values()), len(records) - len(latest)


def _paths(cfg: Config, class_name: str, stem: str, accepted: bool
           ) -> tuple[Path, Path]:
    """某状态下该样本应有的 (图, 掩膜) 路径。"""
    cls = safe_name(class_name)
    if accepted:
        img = cfg.out_path("images") / cls / f"{stem}.png"
        mask = cfg.out_path("masks") / cls / f"{stem}_mask.png"
    else:
        rej = cfg.resolve(cfg.output.get("rejected", "outputs/rejected"))
        img = rej / cls / f"{stem}.png"
        mask = rej / cls / f"{stem}_mask.png"
    return img, mask


def evaluate(cfg: Config, only_classes: list[str] | None = None
             ) -> tuple[list[Verdict], int]:
    """只算不改: 返回每条记录在当前配置下的判定结果。"""
    records = _read_records(cfg.out_path("annotations"))
    records, dropped = _dedupe(records)

    verdicts: list[Verdict] = []
    for r in records:
        cls = r.get("class", "")
        if only_classes and cls not in only_classes:
            continue
        qc = r.get("qc", {}) or {}
        scores = qc.get("scores") or {}
        stem = Path(r["generated_image"]).stem
        was_accepted = r.get("accepted_as", "ok") != "rejected"

        missing = [k for k in quality_check.DIMS if k not in scores]
        if missing:
            fr = [str(x) for x in (qc.get("fail_reasons") or [])]
            note = ("质检未完成(需重跑)"
                    if any("质检未完成" in x for x in fr)
                    else "缺维度: " + ",".join(missing))
            old = float(qc.get("realism", 0) or 0)
            verdicts.append(Verdict(stem, cls, was_accepted, was_accepted,
                                    old, old, [], r, decidable=False,
                                    note=note))
            continue

        passed, realism, fails = quality_check.judge(
            cfg, scores,
            defect_present=bool(qc.get("defect_present", True)),
            only_local=bool(qc.get("only_local", True)),
            # 旧记录没存 on_bottle, 从失败原因里还原
            on_bottle=bool(qc.get("on_bottle",
                                  not any("瓶身" in str(f)
                                          for f in qc.get("fail_reasons", [])))),
            bg_change=float(qc.get("bg_change", 0.0) or 0.0))
        verdicts.append(Verdict(stem, cls, was_accepted, passed,
                                float(qc.get("realism", 0) or 0), realism,
                                fails, r))
    return verdicts, dropped


def _relocate(src: Path, dst: Path) -> str:
    if dst.exists() and src.resolve() == dst.resolve():
        return "already"
    if not src.exists():
        return "missing"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return "moved"


def requalify(cfg: Config, apply: bool = False,
              only_classes: list[str] | None = None) -> list[Verdict]:
    verdicts, dropped = evaluate(cfg, only_classes)
    if not verdicts:
        print("annotations.jsonl 为空或没有匹配的记录")
        return []

    decidable = [v for v in verdicts if v.decidable]
    undecidable = [v for v in verdicts if not v.decidable]
    promoted = [v for v in decidable if v.changed and v.now_accepted]
    demoted = [v for v in decidable if v.changed and not v.now_accepted]

    print(f"记录 {len(verdicts)} 条"
          + (f" (已按 stem 去重, 丢弃 {dropped} 条历史重复)" if dropped else ""))
    print(f"可离线复判 {len(decidable)} 条 / 不可复判 {len(undecidable)} 条")
    floor = cfg.qc.get("min_realism_score", 7)
    print(f"当前阈值: 综合分>={floor}, min_scores={cfg.qc.get('min_scores', {})}")

    if promoted:
        print(f"\n[捞回] 驳回 -> 合格 ({len(promoted)} 条):")
        for v in promoted:
            print(f"  {v.realism_old:>5} -> {v.realism_new:<5} {v.stem}")
    if demoted:
        print(f"\n[踢出] 合格 -> 驳回 ({len(demoted)} 条):")
        for v in demoted:
            print(f"  {v.realism_old:>5} -> {v.realism_new:<5} {v.stem}"
                  f"  因: {'; '.join(v.fails)}")
    if not promoted and not demoted:
        print("\n当前阈值下没有样本需要改判")

    if undecidable:
        why = Counter(v.note for v in undecidable)
        print(f"\n[不可复判] {len(undecidable)} 条(维持原状态), 原因分布:")
        for k, n in why.most_common(5):
            print(f"  {n:>3} 条  {k}")
        print("  这些记录缺少当前判定所需的维度分数, 需重跑或在线补判。")

    ok_now = sum(1 for v in verdicts if (v.now_accepted if v.decidable
                                         else v.was_accepted))
    print(f"\n复判后训练集样本数: {ok_now} / {len(verdicts)}")

    if not apply:
        print("\n(试运行, 未改动任何文件。确认无误后加 --apply 落地)")
        return verdicts

    # ---- 落地: 挪文件 + 重写标注 ----
    ann_path = cfg.out_path("annotations")
    shutil.copy2(ann_path, ann_path.with_suffix(".jsonl.bak"))
    moved = Counter()
    for v in decidable:
        if v.changed:
            src_img, src_mask = _paths(cfg, v.class_name, v.stem, v.was_accepted)
            dst_img, dst_mask = _paths(cfg, v.class_name, v.stem, v.now_accepted)
            # 记录里的实际路径优先(历史目录结构可能不同)
            rec_img = Path(v.record.get("generated_image", src_img))
            rec_mask = Path(v.record.get("mask", src_mask))
            moved[_relocate(rec_img if rec_img.exists() else src_img, dst_img)] += 1
            mask_src = rec_mask if rec_mask.exists() else src_mask
            mask_state = _relocate(mask_src, dst_mask)
            moved[mask_state] += 1
            v.record["generated_image"] = str(dst_img)
            # 驳回样本不再落掩膜, 因此捞回时可能没有掩膜可搬
            if mask_state == "missing" and v.now_accepted:
                v.record["mask"] = ""
                v.record["mask_missing"] = True
                print(f"  [注意] {v.stem} 捞回但无掩膜文件(驳回样本不存掩膜), "
                      f"如需掩膜请重跑该样本")
            else:
                v.record["mask"] = str(dst_mask)
            v.record["accepted_as"] = "ok" if v.now_accepted else "rejected"
        qc = v.record.setdefault("qc", {})
        qc["passed"] = v.now_accepted
        qc["realism"] = v.realism_new
        qc["fail_reasons"] = v.fails
        v.record["requalified"] = True

    with open(ann_path, "w", encoding="utf-8") as f:
        for v in verdicts:
            f.write(json.dumps(v.record, ensure_ascii=False) + "\n")

    print(f"\n[done] 已落地: 文件移动 {moved['moved']} 个"
          f"(缺失 {moved['missing']} 个), 标注已重写")
    print(f"       备份: {ann_path.with_suffix('.jsonl.bak')}")
    return verdicts
