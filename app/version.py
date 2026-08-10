"""版本号单一来源。

构建脚本(packaging/build.ps1)读取这里的值注入 exe 版本资源与 Inno Setup 脚本,
因此不要在别处再维护一份版本号。
"""
from __future__ import annotations

__version__ = "1.0.0"

APP_NAME = "DefectSynth"
APP_DISPLAY_NAME = "缺陷合成流水线"
APP_PUBLISHER = "yolo-dataAugmentation"


def version_tuple() -> tuple[int, int, int]:
    parts = __version__.split(".")
    while len(parts) < 3:
        parts.append("0")
    return tuple(int(p) for p in parts[:3])  # type: ignore[return-value]
