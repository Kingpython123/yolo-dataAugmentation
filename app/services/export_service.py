"""成果导出。

替代 README 第五节那套手工步骤: 手动挑出 generated/ 与 rejected/ 打包、记着
带上 annotations.jsonl、还要记着跳过约 2 GB 的 debug/。这三件事漏任何一件都
会出问题(少交东西, 或者压缩包大出一个数量级)。

设计取舍: 排除清单是白名单之外的硬编码黑名单, 而不是"让用户自己勾选目录"。
因为 debug/ 的存在意义就是排查问题, 从来不属于交付物, 把它做成选项只会给
误操作留口子。
"""
from __future__ import annotations

import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from src.config import load_config
from src.dataset import safe_name

# 需要交付的内容(requirements.md FR-6.1)。
# rejected 一起交是刻意的: 质检阈值偏严, 里面有相当比例是"质量可用、只是某一项
# 分数差一点"被拦下的, 后续人工统一筛选。
EXPORT_TREES = ("generated", "rejected")
EXPORT_FILES = ("annotations",)

# 强制排除的中间产物(FR-6.2)
EXCLUDED_DIRS = ("debug", "debug_preview", "seg_preview", "selftest",
                 "structure_preview", "gen_ref_review")

ProgressCallback = Callable[[int, int, str], None]


@dataclass
class ExportEstimate:
    file_count: int = 0
    total_bytes: int = 0
    per_tree: dict = field(default_factory=dict)   # 目录名 -> (文件数, 字节数)
    classes: list = field(default_factory=list)
    missing: list = field(default_factory=list)    # 不存在的目录, 供提示
    annotations_lines: int = 0

    @property
    def total_mb(self) -> float:
        return self.total_bytes / 1024 / 1024

    @property
    def summary(self) -> str:
        if not self.file_count:
            return "没有可导出的内容"
        return (f"{self.file_count} 个文件, 约 {self.total_mb:.0f} MB"
                f"(压缩后通常更小)")


def _tree_dirs(cfg) -> dict:
    """产物目录。键名与 config.yaml 的 output.* 对应。"""
    root = cfg.resolve(cfg.output.get("root", "outputs"))
    return {
        "generated": cfg.resolve(cfg.output.get("images", "outputs/generated")),
        "rejected": cfg.resolve(cfg.output.get("rejected", "outputs/rejected")),
        "masks": cfg.resolve(cfg.output.get("masks", "outputs/masks")),
        "root": root,
    }


def _annotations_path(cfg) -> Path:
    return cfg.resolve(cfg.output.get("annotations",
                                     "outputs/annotations.jsonl"))


def estimate(classes: list | None = None, include_masks: bool = False,
             cfg=None) -> ExportEstimate:
    """预估导出体积与文件数。classes 为空表示全部类别。"""
    cfg = cfg or load_config()
    result = ExportEstimate()
    dirs = _tree_dirs(cfg)
    wanted = {safe_name(c) for c in classes} if classes else None

    trees = list(EXPORT_TREES) + (["masks"] if include_masks else [])
    for tree in trees:
        base = dirs[tree]
        if not base.exists():
            result.missing.append(tree)
            continue
        count = 0
        size = 0
        for class_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            if wanted is not None and class_dir.name not in wanted:
                continue
            if class_dir.name not in result.classes:
                result.classes.append(class_dir.name)
            for f in class_dir.rglob("*"):
                if f.is_file():
                    count += 1
                    size += f.stat().st_size
        result.per_tree[tree] = (count, size)
        result.file_count += count
        result.total_bytes += size

    ann = _annotations_path(cfg)
    if ann.exists():
        result.file_count += 1
        result.total_bytes += ann.stat().st_size
        with open(ann, "r", encoding="utf-8", errors="replace") as f:
            result.annotations_lines = sum(1 for line in f if line.strip())
    else:
        result.missing.append("annotations")
    return result


def export(dest: Path | str, classes: list | None = None,
           include_masks: bool = False,
           progress: ProgressCallback | None = None,
           cfg=None) -> Path:
    """打包成果到 dest(.zip)。返回实际写出的路径。"""
    cfg = cfg or load_config()
    dest = Path(dest)
    if dest.suffix.lower() != ".zip":
        dest = dest.with_suffix(".zip")
    dest.parent.mkdir(parents=True, exist_ok=True)

    dirs = _tree_dirs(cfg)
    wanted = {safe_name(c) for c in classes} if classes else None
    trees = list(EXPORT_TREES) + (["masks"] if include_masks else [])

    members: list[tuple[Path, str]] = []
    for tree in trees:
        base = dirs[tree]
        if not base.exists():
            continue
        for class_dir in sorted(p for p in base.iterdir() if p.is_dir()):
            if wanted is not None and class_dir.name not in wanted:
                continue
            for f in sorted(class_dir.rglob("*")):
                if not f.is_file():
                    continue
                if _is_excluded(f, base):
                    continue
                members.append((f, f"{tree}/{f.relative_to(base).as_posix()}"))

    ann = _annotations_path(cfg)
    if ann.exists():
        members.append((ann, ann.name))

    total = len(members)
    if not total:
        raise RuntimeError("没有可导出的内容: generated/ 与 rejected/ 都是空的")

    # PNG 已是压缩格式, 再 deflate 收益很小却显著拖慢导出(几万张图的量级)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for i, (path, arcname) in enumerate(members, 1):
            compress = (zipfile.ZIP_STORED
                        if path.suffix.lower() in (".png", ".jpg", ".jpeg",
                                                   ".bmp", ".webp")
                        else zipfile.ZIP_DEFLATED)
            zf.write(path, arcname, compress_type=compress)
            if progress and (i % 100 == 0 or i == total):
                progress(i, total, f"打包 {arcname}")
    return dest


def _is_excluded(path: Path, base: Path) -> bool:
    """路径里是否含被排除的中间产物目录。"""
    try:
        parts = path.relative_to(base).parts
    except ValueError:
        parts = path.parts
    return any(part in EXCLUDED_DIRS for part in parts)


def default_dest_name() -> str:
    import time
    return f"defect-synth-成果-{time.strftime('%Y%m%d-%H%M')}.zip"
