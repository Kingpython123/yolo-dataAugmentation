"""数据集扫描: 枚举类别及每类图片路径。"""
from __future__ import annotations

from pathlib import Path

from .config import Config


def safe_name(name: str) -> str:
    """类别名/条目名转为可安全用作目录与文件名的形式(全角括号->半角)。"""
    return name.replace("（", "(").replace("）", ")").replace("/", "_")


def list_classes(cfg: Config, root: Path) -> list[str]:
    configured = cfg.data.get("classes") or []
    if configured:
        return [c for c in configured if (root / c).is_dir()]
    return sorted([p.name for p in root.iterdir() if p.is_dir()])


def _iter_images(folder: Path, exts: list[str]) -> list[Path]:
    exts_l = {e.lower() for e in exts}
    out: list[Path] = []
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in exts_l:
            out.append(p)
    return out


def scan_class_images(cfg: Config, root: Path) -> dict[str, list[Path]]:
    """返回 {类别名: [图片路径, ...]}。"""
    exts = cfg.data.get("extensions", [".bmp", ".jpg", ".jpeg", ".png"])
    result: dict[str, list[Path]] = {}
    for cls in list_classes(cfg, root):
        imgs = _iter_images(root / cls, exts)
        if imgs:
            result[cls] = imgs
    return result
