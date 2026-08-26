# -*- coding: utf-8 -*-
"""把历史产出的文件名同步到新类别名/新 entry_id。

为什么必须做
------------
产出文件名本身编码了"哪个类别 + 哪张干净图 + 哪条参考", src/rejection.py 正是靠
它做反查(regen-rejected / gen-augment 方式C 都依赖):
    {class}__{clean_stem}__t{idx}__ref_{entry_id}
    {class}__{clean_stem}__ref_{entry_id}
tools/migrate_classes_by_product.py 只改了目录名、缺陷库和标注, 文件名里的
`1.jpg__` 前缀与 `__ref_5.4__...` 片段还是旧的。结果是:
  - _split_head() 拿新类别列表去匹配旧前缀 -> "无法从文件名识别出已知类别"
  - 即便类别对上, ref 片段查缺陷库索引也会失败(entry_id 已改)
即历史产出的反查会全部失效。这里把文件名一并迁移。

依赖 outputs/product_migration_reverse_map.json 里的 id_map(旧 entry_id -> 新)。

用法:
  python tools/migrate_output_filenames.py           # 试算
  python tools/migrate_output_filenames.py --apply
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUTPUTS = REPO / "outputs"
REV = OUTPUTS / "product_migration_reverse_map.json"

# 与 dataset.safe_name 一致
def safe_name(name: str) -> str:
    return name.replace("（", "(").replace("）", ")").replace("/", "_")


CLEAN_MAP = {
    "1.jpg": "500多效极润_角度1", "2.jpg": "500多效极润_角度2",
    "3.jpg": "500多效极润_角度3", "4.jpg": "500多效极润_角度4",
    "1.bmp": "60ml多效极润_角度1", "2.bmp": "60ml多效极润_角度2",
    "3.bmp": "60ml多效极润_角度3", "4.bmp": "60ml多效极润_角度4",
    "5.1": "500水次方_角度1", "5.2": "500水次方_角度2",
    "5.3": "500水次方_角度3", "5.4": "500水次方_角度4",
    "120蓝瓶": "120蓝瓶_角度4",
}


def build_ref_map() -> dict[str, str]:
    """旧 safe(entry_id) -> 新 safe(entry_id)。"""
    if not REV.is_file():
        sys.exit(f"[ERROR] 找不到 {REV}, 请先跑 migrate_classes_by_product.py --apply")
    id_map = json.loads(REV.read_text(encoding="utf-8"))["id_map"]
    return {safe_name(a): safe_name(b) for a, b in id_map.items()}


# 原来是"产品/角度子目录"结构的产品: 旧产出的类别前缀是产品名(没有角度),
# 光看文件名判断不出角度, 只能靠干净图 stem 去现存数据集里反查。
NESTED_PRODUCTS = ("360森呼吸",)


def build_stem_to_class(clean_root: Path) -> dict[str, str]:
    """干净图 stem -> 新类别名。用于给旧的产品级前缀补出角度。"""
    out: dict[str, str] = {}
    if not clean_root.is_dir():
        return out
    for d in clean_root.iterdir():
        if not d.is_dir():
            continue
        if not any(d.name.startswith(p + "_角度") for p in NESTED_PRODUCTS):
            continue
        for p in d.iterdir():
            if p.is_file():
                out.setdefault(p.stem, d.name)
    return out


def new_name(name: str, ref_map: dict[str, str],
             stem_to_class: dict[str, str] | None = None) -> str | None:
    """算出新文件名; 无需改动时返回 None。"""
    out = name
    # 1) 开头的类别段: 按最长优先, 避免 "1.jpg" 误配到别的前缀
    matched = False
    for old in sorted(CLEAN_MAP, key=len, reverse=True):
        if out.startswith(old + "__"):
            out = CLEAN_MAP[old] + out[len(old):]
            matched = True
            break
    # 1b) 产品级前缀(旧的嵌套结构): 用干净图 stem 反查角度。查不到就**不改**,
    #     不猜、不伪造 —— 这类产出的源图已随演示目录一起删掉了。
    if not matched and stem_to_class:
        for prod in NESTED_PRODUCTS:
            pre = prod + "__"
            if out.startswith(pre):
                rest = out[len(pre):]
                stem = rest.split("__", 1)[0]
                cls = stem_to_class.get(stem)
                if cls:
                    out = cls + "__" + rest
                break
    # 2) __ref_<entry_id> 片段
    marker = "__ref_"
    i = out.find(marker)
    if i >= 0:
        head, tail = out[:i + len(marker)], out[i + len(marker):]
        # tail 可能带后缀(.png / _mask.png), 逐个候选做前缀匹配, 最长优先
        for old in sorted(ref_map, key=len, reverse=True):
            if tail.startswith(old):
                tail = ref_map[old] + tail[len(old):]
                break
        out = head + tail
    return out if out != name else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    ref_map = build_ref_map()
    print(f"参考条目改名表: {len(ref_map)} 条")

    import yaml
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    clean_root = Path(cfg["data"]["clean_root"])
    if not clean_root.is_absolute():
        clean_root = (REPO / clean_root).resolve()
    stem_to_class = build_stem_to_class(clean_root)
    print(f"用于补角度的干净图 stem 索引: {len(stem_to_class)} 条 "
          f"(仅覆盖 {NESTED_PRODUCTS})")

    targets: list[tuple[Path, Path]] = []
    unmatched: list[Path] = []
    still_product_level: list[Path] = []
    for sub in ("generated", "masks", "rejected", "regenerated"):
        base = OUTPUTS / sub
        if not base.is_dir():
            continue
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            nn = new_name(p.name, ref_map, stem_to_class)
            if nn:
                # 类别段仍是产品级(角度没补上)的, 单独记账
                if any(nn.startswith(prod + "__") for prod in NESTED_PRODUCTS):
                    still_product_level.append(p)
                targets.append((p, p.with_name(nn)))
            elif "__ref_" in p.name:
                unmatched.append(p)

    # debug 下是"每个任务一个子目录", 目录名同样嵌了类别与 entry_id
    dbg = OUTPUTS / "debug"
    dir_targets: list[tuple[Path, Path]] = []
    if dbg.is_dir():
        for cls_dir in dbg.iterdir():
            if not cls_dir.is_dir():
                continue
            for d in cls_dir.iterdir():
                if not d.is_dir():
                    continue
                nn = new_name(d.name, ref_map, stem_to_class)
                if nn:
                    dir_targets.append((d, d.with_name(nn)))

    print(f"\n需改名的产出文件: {len(targets)}")
    print(f"需改名的 debug 任务目录: {len(dir_targets)}")
    print(f"含 __ref_ 但没能匹配上的文件: {len(unmatched)}")
    for p in unmatched[:5]:
        print(f"  未匹配: {p.relative_to(OUTPUTS)}")
    if still_product_level:
        print(f"\n[已知无法补角度] {len(still_product_level)} 个文件的类别段仍是产品级:")
        print("  原因: 它们的干净图来自已删除的 演示素材_四类瓶子褶皱对比 目录,")
        print("  stem 在现存数据集里查不到, 角度不可知。不猜、保持产品级前缀。")
        for p in still_product_level[:4]:
            print(f"    {p.name[:70]}")

    for a, b in targets[:4]:
        print(f"\n  {a.name}\n->{b.name}")

    if not args.apply:
        print("\n试算完成, 未做修改。加 --apply 执行。")
        return 0

    done = 0
    for a, b in targets + dir_targets:
        if not a.exists():
            continue
        if b.exists():
            print(f"  [跳过] 目标已存在: {b.name}")
            continue
        a.rename(b)
        done += 1
    print(f"\n已改名 {done} 项")

    # 标注里的 generated_image / mask 路径也要跟着换文件名
    ann_paths = [OUTPUTS / "annotations.jsonl",
                 REPO.parent / "测试8.13" / "annotations.jsonl",
                 REPO.parent / "测试8.13_batch1" / "annotations.jsonl"]
    for ap_ in ann_paths:
        if not ap_.is_file():
            continue
        lines = ap_.read_text(encoding="utf-8").splitlines()
        out, changed = [], 0
        for line in lines:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            for key in ("generated_image", "mask"):
                v = rec.get(key)
                if not v:
                    continue
                p = Path(str(v))
                nn = new_name(p.name, ref_map, stem_to_class)
                if nn:
                    rec[key] = str(p.with_name(nn))
                    changed += 1
            # 产品级 class(旧嵌套结构遗留)也用 stem 补出角度
            cls = str(rec.get("class", ""))
            if cls in NESTED_PRODUCTS:
                stem = Path(str(rec.get("clean_source", ""))).stem
                new_cls = stem_to_class.get(stem)
                if new_cls:
                    rec["class"] = new_cls
                    # clean_source 原本指向已删除的演示目录, 一并指回真实数据集
                    real = clean_root / new_cls
                    for cand in real.glob(stem + ".*"):
                        rec["clean_source"] = str(cand)
                        break
                    changed += 1
            out.append(json.dumps(rec, ensure_ascii=False))
        if changed:
            ap_.write_text("\n".join(out) + "\n", encoding="utf-8")
            print(f"  标注 {ap_.name}: 修正 {changed} 个文件名引用")
    return 0


if __name__ == "__main__":
    sys.exit(main())
