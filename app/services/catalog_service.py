"""缺陷库: 体检与数据包安装。

体检逻辑不在这里实现, 而是复用 src/catalog_report.py —— 那是 CLI 的 inspect
命令用的同一份判定, 界面与命令行看到的结论因此永远一致。
"""
from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app.runtime import paths, settings
from src import catalog_report
from src.catalog_report import CatalogReport
from src.config import load_config

# 与 packaging/make_catalog_pack.py 约定一致
PACK_ROOT = "catalog"
MANIFEST_NAME = "manifest.json"

ProgressCallback = Callable[[int, int, str], None]


class PackError(RuntimeError):
    """数据包不可用(缺 manifest、校验不过、结构不对等)。"""


@dataclass
class PackInfo:
    path: Path
    entry_count: int = 0
    crop_count: int = 0
    total_bytes: int = 0
    created_at: str = ""
    source_commit: str = ""
    files: dict = field(default_factory=dict)

    @property
    def summary(self) -> str:
        mb = self.total_bytes / 1024 / 1024
        return (f"{self.entry_count} 条缺陷 / {self.crop_count} 张裁剪图 / "
                f"{mb:.0f} MB, 制作于 {self.created_at or '未知时间'}")


def inspect() -> CatalogReport:
    """当前工作区里缺陷库的体检结果。"""
    return catalog_report.build_report(load_config())


def read_pack_info(zip_path: Path | str) -> PackInfo:
    """读取数据包的 manifest, 不解压。用于安装前给用户看清要装什么。"""
    zip_path = Path(zip_path)
    if not zip_path.exists():
        raise PackError(f"文件不存在: {zip_path}")
    try:
        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            if MANIFEST_NAME not in names:
                raise PackError(
                    f"这不是缺陷库数据包(缺少 {MANIFEST_NAME})。请选择由 "
                    f"packaging/make_catalog_pack.py 生成的 catalog-data.zip")
            manifest = json.loads(zf.read(MANIFEST_NAME).decode("utf-8"))
            if f"{PACK_ROOT}/catalog.json" not in names:
                raise PackError(f"数据包结构不对: 缺少 {PACK_ROOT}/catalog.json")
    except zipfile.BadZipFile as e:
        raise PackError(f"压缩包已损坏: {e}") from e
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise PackError(f"manifest 无法解析: {e}") from e

    return PackInfo(
        path=zip_path,
        entry_count=int(manifest.get("entry_count") or 0),
        crop_count=int(manifest.get("crop_count") or 0),
        total_bytes=int(manifest.get("total_bytes") or 0),
        created_at=str(manifest.get("created_at") or ""),
        source_commit=str(manifest.get("source_commit") or ""),
        files=manifest.get("files") or {},
    )


def install_pack(zip_path: Path | str,
                 progress: ProgressCallback | None = None) -> CatalogReport:
    """把数据包安装到 工作区/outputs/catalog/。

    先解压到同一磁盘上的临时目录再整体替换, 而不是直接往目标目录里解压:
    解压 440 MB 中途失败(磁盘满、被杀进程)会留下一个"看起来装好了但缺文件"
    的缺陷库, 那种状态要到生成跑到一半才暴露。
    """
    info = read_pack_info(zip_path)
    target = paths.catalog_dir()
    target.parent.mkdir(parents=True, exist_ok=True)

    free = paths.disk_free_bytes(target.parent)
    # 解压过程中临时目录与目标目录会短暂共存, 因此要留出两倍余量
    needed = info.total_bytes * 2
    if free and free < needed:
        raise PackError(
            f"磁盘空间不足: 需要约 {needed / 1024 ** 3:.1f} GB, "
            f"当前可用 {free / 1024 ** 3:.1f} GB")

    staging = Path(tempfile.mkdtemp(prefix="catalog_pack_",
                                   dir=str(target.parent)))
    try:
        with zipfile.ZipFile(zip_path) as zf:
            members = [n for n in zf.namelist()
                       if n.startswith(PACK_ROOT + "/") and not n.endswith("/")]
            total = len(members)
            for i, name in enumerate(members, 1):
                # 防目录穿越: 压缩包里的路径不可信
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts:
                    raise PackError(f"数据包含非法路径: {name}")
                zf.extract(name, staging)
                if progress and (i % 50 == 0 or i == total):
                    progress(i, total, "解压缺陷库")

        extracted = staging / PACK_ROOT
        if not (extracted / "catalog.json").exists():
            raise PackError("解压后没有 catalog.json, 数据包可能不完整")

        missing = _verify(extracted, info)
        if missing:
            raise PackError(
                f"数据包校验未通过: {len(missing)} 个文件缺失或内容不符, "
                f"前几个: {', '.join(missing[:3])}")

        if progress:
            progress(total, total, "替换旧缺陷库")
        if target.exists():
            backup = target.with_name(target.name + ".old")
            shutil.rmtree(backup, ignore_errors=True)
            target.rename(backup)
            shutil.rmtree(backup, ignore_errors=True)
        extracted.rename(target)
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    settings.update(catalog_installed_at=info.created_at)
    return inspect()


def _verify(extracted: Path, info: PackInfo) -> list:
    """按 manifest 里的 sha256 校验解压结果。

    只校验 manifest 声明过的文件。哈希不匹配与文件缺失同等对待 —— 两者都会让
    生成流程拿到错误的参考图, 而那种问题在产出图上很难看出来。
    """
    import hashlib

    bad: list = []
    for name, expected in info.files.items():
        if not name.startswith(PACK_ROOT + "/"):
            continue
        relative = name[len(PACK_ROOT) + 1:]
        path = extracted / relative
        if not path.exists():
            bad.append(relative)
            continue
        h = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                block = f.read(1 << 20)
                if not block:
                    break
                h.update(block)
        if h.hexdigest() != expected:
            bad.append(relative + "(内容不符)")
    return bad
