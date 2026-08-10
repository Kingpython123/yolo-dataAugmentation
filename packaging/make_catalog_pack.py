"""制作缺陷库数据包 catalog-data.zip。

为什么缺陷库要外置分发(requirements.md C4/D2):
  outputs/catalog 共 676 个文件、约 440 MB。塞进安装包会让安装包体积翻好几倍,
  而缺陷库是相对稳定的数据(需求 N2 明确不重建), 与程序版本解耦更合理。

顺带修掉一个可移植性缺陷(C5/FR-4.4):
  catalog.json 里有一批条目的 source_image 是建库那台机器的绝对路径, 形如
  D:\\zlf\\project\\photo_difussion\\实拍正样本（有缺陷）\\5.4\\xxx.jpg
  当前不影响裁块计算(因为同时存在 source_size 字段兜底), 但分发前应当归一化,
  否则别人机器上这个字段完全没有意义, 排查问题时还会误导。

用法:
  python packaging/make_catalog_pack.py --dry-run          # 只体检与试算, 不打包
  python packaging/make_catalog_pack.py -o dist/catalog-data.zip
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

# 包内目录名。安装时整体解压到 工作区/outputs/ 下, 因此这里用 catalog/
PACK_ROOT = "catalog"
MANIFEST_NAME = "manifest.json"

CATALOG_JSON = "catalog.json"
ANALYZED_JSON = "analyzed.json"
CROPS_DIR = "crops"

MANIFEST_VERSION = 1

# 已压缩的 PNG 再 deflate 收益极小却很耗时, 直接存储
STORED_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            block = f.read(chunk)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=REPO, capture_output=True, text=True,
                             timeout=10, check=False)
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def normalize_source_image(records: list, defect_root: str) -> tuple[list, int]:
    """把 source_image 统一成 `<defect_root>/<class_name>/<文件名>` 形式。

    以 class_name + 文件名重建而不是做路径字符串替换: 原始值里既有 Windows
    反斜杠绝对路径也有 POSIX 相对路径, 逐一处理分隔符差异容易漏, 而这两个
    字段本身就足以确定文件位置。
    """
    changed = 0
    prefix = defect_root.rstrip("/\\").replace("\\", "/")
    for rec in records:
        raw = str(rec.get("source_image") or "")
        if not raw:
            continue
        filename = raw.replace("\\", "/").rsplit("/", 1)[-1]
        cls = str(rec.get("class_name") or "")
        if not filename or not cls:
            continue
        wanted = f"{prefix}/{cls}/{filename}"
        if raw.replace("\\", "/") != wanted:
            rec["source_image"] = wanted
            changed += 1
    return records, changed


def collect(catalog_dir: Path) -> tuple[Path, Path | None, list]:
    catalog_json = catalog_dir / CATALOG_JSON
    if not catalog_json.exists():
        raise SystemExit(f"找不到缺陷库: {catalog_json}")
    analyzed = catalog_dir / ANALYZED_JSON
    crops = sorted((catalog_dir / CROPS_DIR).glob("*"))
    crops = [p for p in crops if p.is_file()]
    return catalog_json, (analyzed if analyzed.exists() else None), crops


def build_manifest(records: list, crops: list, catalog_bytes: bytes,
                   catalog_dir: Path) -> dict:
    files: dict[str, str] = {
        f"{PACK_ROOT}/{CATALOG_JSON}": hashlib.sha256(catalog_bytes).hexdigest(),
    }
    for p in crops:
        files[f"{PACK_ROOT}/{CROPS_DIR}/{p.name}"] = sha256_of(p)
    return {
        "manifest_version": MANIFEST_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_commit": git_commit(),
        "entry_count": len(records),
        "crop_count": len(crops),
        "total_bytes": sum(p.stat().st_size for p in crops) + len(catalog_bytes),
        "files": files,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="制作缺陷库数据包")
    ap.add_argument("-o", "--output", default="dist/catalog-data.zip",
                    help="输出的 zip 路径")
    ap.add_argument("--catalog-dir", default=None,
                    help="缺陷库目录(默认 <仓库>/outputs/catalog)")
    ap.add_argument("--defect-root", default=None,
                    help="归一化 source_image 用的前缀(默认取 config.yaml 的 "
                         "data.defect_root)")
    ap.add_argument("--dry-run", action="store_true",
                    help="只体检与试算, 不产出 zip")
    args = ap.parse_args()

    catalog_dir = (Path(args.catalog_dir) if args.catalog_dir
                   else REPO / "outputs" / "catalog")
    catalog_json, analyzed, crops = collect(catalog_dir)

    records = json.loads(catalog_json.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise SystemExit("catalog.json 内容不是数组")

    defect_root = args.defect_root
    if defect_root is None:
        import yaml
        cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
        defect_root = (cfg.get("data") or {}).get(
            "defect_root", "../实拍正样本（有缺陷）")

    absolute_before = sum(
        1 for r in records
        if ":" in str(r.get("source_image", ""))[:3]
        or str(r.get("source_image", "")).startswith("\\\\"))
    records, changed = normalize_source_image(records, defect_root)

    # 裁剪图缺失是硬错误: 装到别人机器上会让生成流程拿不到参考图
    missing = [r.get("entry_id") for r in records
               if not (catalog_dir / str(r.get("crop_path", "")).replace(
                   "outputs/catalog/", "")).exists()]

    print(f"缺陷库目录 : {catalog_dir}")
    print(f"条目数     : {len(records)}")
    print(f"裁剪图     : {len(crops)} 个")
    print(f"source_image 归一化: {changed} 条被改写 "
          f"(其中原本是绝对路径的约 {absolute_before} 条)")
    print(f"体积       : {sum(p.stat().st_size for p in crops) / 1024 / 1024:.1f} MB")
    if missing:
        print(f"[错误] {len(missing)} 条裁剪图缺失, 前 5 条: {missing[:5]}")
        return 1

    catalog_bytes = json.dumps(records, ensure_ascii=False,
                               indent=1).encode("utf-8")
    manifest = build_manifest(records, crops, catalog_bytes, catalog_dir)

    if args.dry_run:
        print("\n[dry-run] 未产出文件。manifest 摘要:")
        preview = {k: v for k, v in manifest.items() if k != "files"}
        print(json.dumps(preview, ensure_ascii=False, indent=2))
        return 0

    output = Path(args.output)
    if not output.is_absolute():
        output = REPO / output
    output.parent.mkdir(parents=True, exist_ok=True)

    print(f"\n正在写入 {output} ...")
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED,
                         compresslevel=1) as zf:
        zf.writestr(MANIFEST_NAME,
                    json.dumps(manifest, ensure_ascii=False, indent=1))
        zf.writestr(f"{PACK_ROOT}/{CATALOG_JSON}", catalog_bytes)
        if analyzed is not None:
            zf.write(analyzed, f"{PACK_ROOT}/{ANALYZED_JSON}")
        for i, p in enumerate(crops, 1):
            compress = (zipfile.ZIP_STORED if p.suffix.lower() in STORED_SUFFIXES
                        else zipfile.ZIP_DEFLATED)
            zf.write(p, f"{PACK_ROOT}/{CROPS_DIR}/{p.name}",
                     compress_type=compress)
            if i % 100 == 0:
                print(f"  {i}/{len(crops)}")

    size_mb = output.stat().st_size / 1024 / 1024
    print(f"完成: {output} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
