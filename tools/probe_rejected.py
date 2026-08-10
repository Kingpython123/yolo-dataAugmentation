"""诊断: 能否从"人工驳回"的文件名反查出 (类别, 干净图, 缺陷库条目)。

这是新功能可行性的前提。若反查不可靠, 就必须要求用户提供 annotations.jsonl,
方案会完全不同。

反查思路: 不做字符串逆运算(safe_name 把 （）/ 都替换掉了, 逆运算有歧义),
而是正向给缺陷库每条目算一次 safe_name(entry_id) 建索引, 再用文件名里的
ref 片段去查表 —— 这样是精确匹配, 没有歧义。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.dataset import safe_name  # noqa: E402

REJECTED_DIR = Path(r"D:\zlf\photo_difussion\没打标签的")

# generate_target 的命名规则:
#   f"{safe(class)}__{safe(clean.stem)}__t{i}__ref_{safe(entry_id)}"
STEM_RE = re.compile(r"^(?P<head>.+?)__t(?P<idx>\d+)__ref_(?P<ref>.+)$")


def main() -> int:
    cfg_path = REPO / "config.yaml"
    import yaml
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    clean_root = Path(cfg["data"]["clean_root"])

    catalog = json.loads(
        (REPO / "outputs" / "catalog" / "catalog.json").read_text(encoding="utf-8"))
    # 正向索引: safe_name(entry_id) -> entry_id
    by_safe: dict[str, str] = {}
    collisions = 0
    for rec in catalog:
        key = safe_name(rec["entry_id"])
        if key in by_safe:
            collisions += 1
        by_safe[key] = rec["entry_id"]
    print(f"缺陷库条目: {len(catalog)}, safe_name 索引: {len(by_safe)}, "
          f"键冲突: {collisions}")

    known_classes = sorted([p.name for p in clean_root.iterdir() if p.is_dir()],
                          key=len, reverse=True)
    print(f"数据集类别: {len(known_classes)} 个")
    exts = [e.lower() for e in cfg["data"]["extensions"]]

    files = sorted(REJECTED_DIR.glob("*.png"))
    print(f"\n待复查文件: {len(files)}\n")

    ok = 0
    problems: list[str] = []
    for f in files:
        m = STEM_RE.match(f.stem)
        if not m:
            problems.append(f"文件名不符合命名规则: {f.name}")
            continue
        head, idx, ref_safe = m["head"], int(m["idx"]), m["ref"]

        # head = safe(class) + "__" + safe(clean_stem); 用已知类别名做前缀匹配
        cls = None
        clean_stem = None
        for candidate in known_classes:
            prefix = safe_name(candidate) + "__"
            if head.startswith(prefix):
                cls = candidate
                clean_stem = head[len(prefix):]
                break
        if cls is None:
            problems.append(f"无法识别类别: {f.name}")
            continue

        entry_id = by_safe.get(ref_safe)
        if entry_id is None:
            problems.append(f"缺陷库里找不到参考条目 '{ref_safe}': {f.name}")
            continue

        # 干净图是否还在
        clean_file = None
        for ext in exts:
            candidate = clean_root / cls / f"{clean_stem}{ext}"
            if candidate.exists():
                clean_file = candidate
                break
        if clean_file is None:
            problems.append(f"找不到原始干净图 {cls}/{clean_stem}.*: {f.name}")
            continue

        ok += 1
        if ok <= 5:
            print(f"[OK] {f.name}")
            print(f"     类别={cls}  序号=t{idx}")
            print(f"     干净图={clean_file.name}")
            print(f"     参考条目={entry_id}")

    print(f"\n可完整反查: {ok}/{len(files)}")
    if problems:
        print(f"\n有问题的 {len(problems)} 个:")
        for p in problems[:10]:
            print(f"  - {p}")
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    raise SystemExit(main())
