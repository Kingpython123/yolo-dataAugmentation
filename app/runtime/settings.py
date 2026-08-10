"""用户级配置持久化(%APPDATA%\\DefectSynth\\settings.json)。

这里只存"界面偏好与位置信息", 不存生成算法参数 —— 算法参数继续留在工作区的
config.yaml 里, 保持与改造前同一处可调。也绝不存 API key(见 secrets.py)。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from . import _jsonstore, paths

# 默认并发数与 config.yaml 的 api.max_workers 保持一致
DEFAULT_MAX_WORKERS = 3


@dataclass
class Settings:
    """用户偏好。所有字段都必须可 JSON 序列化。"""

    workspace: str = ""
    clean_root: str = ""          # 无缺陷样本根目录(绝对路径, 替代原来的相对约定)
    base_url: str = ""            # 留空表示沿用 config.yaml 里的值
    max_workers: int = DEFAULT_MAX_WORKERS
    theme: str = "dark"           # dark | light
    last_classes: list[str] = field(default_factory=list)
    last_count: int = 700
    catalog_installed_at: str = ""

    # ---- 序列化 ----

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "Settings":
        """按字段名逐个取值, 忽略未知键。

        这样旧版本写下的多余键不会让新版本启动失败, 新增字段也能自动取默认值。
        """
        if not isinstance(data, dict):
            return cls()
        known = {f.name: f for f in fields(cls)}
        kwargs: dict[str, Any] = {}
        for name, f in known.items():
            if name not in data:
                continue
            value = data[name]
            if name == "last_classes":
                if isinstance(value, list):
                    kwargs[name] = [str(v) for v in value]
                continue
            if name in ("max_workers", "last_count"):
                try:
                    kwargs[name] = int(value)
                except (TypeError, ValueError):
                    pass
                continue
            if isinstance(value, str):
                kwargs[name] = value
        return cls(**kwargs)

    # ---- 派生 ----

    def clean_root_path(self) -> Path | None:
        return Path(self.clean_root).expanduser() if self.clean_root.strip() else None


def load() -> Settings:
    return Settings.from_dict(_jsonstore.read_json(paths.settings_file(), default={}))


def save(settings: Settings) -> None:
    _jsonstore.write_json(paths.settings_file(), settings.to_dict())


def update(**changes: Any) -> Settings:
    """局部更新并落盘, 返回更新后的完整配置。"""
    current = load()
    for key, value in changes.items():
        if not hasattr(current, key):
            raise KeyError(f"未知配置项: {key}")
        setattr(current, key, value)
    save(current)
    return current
