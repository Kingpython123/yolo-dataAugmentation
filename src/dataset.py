"""数据集扫描: 枚举类别及每类图片路径。"""
from __future__ import annotations

from pathlib import Path

from .config import Config


def safe_name(name: str) -> str:
    """类别名/条目名转为可安全用作目录与文件名的形式(全角括号->半角)。"""
    return name.replace("（", "(").replace("）", ")").replace("/", "_")


# 类别命名约定: `<产品>_角度<N>`, 例如 500多效极润_角度1 / 120蓝瓶_角度4。
# 由来: 原来的类别名是 1~4.jpg / 1~4.bmp / 5.1~5.4, 无法看出是哪种瓶子, 而且
# `1~4.bmp` 在两个根目录下指的是**不同产品**(正样本里是 120蓝瓶, 负样本里是
# 60ml多效极润)。流水线按同名类别目录配对、cross_class 又只是目录名字符串比较,
# 于是"拿 60ml白瓶当目标 + 120蓝瓶的缺陷当参考"会被记成同类。改名后产品身份
# 写进类别名, 产品级判断由 product_of() 提供。
ANGLE_SEP = "_角度"


def product_of(class_name: str) -> str:
    """从类别名取出产品名。没有角度后缀时原样返回。

    500多效极润_角度1 -> 500多效极润
    120蓝瓶_角度4     -> 120蓝瓶
    """
    return str(class_name).split(ANGLE_SEP)[0]


def same_product(a: str, b: str) -> bool:
    """两个类别是否属于同一产品(忽略角度差异)。"""
    return product_of(a) == product_of(b)


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
