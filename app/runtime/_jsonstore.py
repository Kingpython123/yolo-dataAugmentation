"""极小的 JSON 文件读写工具。

单独成模块是为了打断 paths <-> settings 的循环依赖: 两者都需要读写同一个
settings.json, 但 paths 不能 import settings(settings 需要 paths 定位文件)。

写入采用"临时文件 + 替换"的方式, 避免进程在写入中途退出时留下半截文件 ——
作业状态与用户配置都可能在长跑期间被反复改写, 半截 JSON 会让下次启动直接失败。
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any = None) -> Any:
    """读取 JSON; 文件不存在或内容损坏时返回 default 而不抛异常。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, NotADirectoryError):
        return default
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return default


def write_json(path: Path, data: Any) -> None:
    """原子写入 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def update_json(path: Path, **changes: Any) -> dict:
    """读-改-写: 合并若干键并落盘, 返回合并后的完整字典。"""
    data = read_json(path, default={})
    if not isinstance(data, dict):
        data = {}
    data.update(changes)
    write_json(path, data)
    return data
