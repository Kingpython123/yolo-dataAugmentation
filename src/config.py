"""配置加载与路径解析。"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, data: dict[str, Any], config_path: Path):
        self._d = data
        self.config_path = config_path
        self.base_dir = config_path.resolve().parent

    # ---- 原始访问 ----
    def __getitem__(self, key: str) -> Any:
        return self._d[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._d.get(key, default)

    # ---- 常用分组 ----
    @property
    def api(self) -> dict[str, Any]:
        return self._d["api"]

    @property
    def models(self) -> dict[str, Any]:
        return self._d["models"]

    @property
    def data(self) -> dict[str, Any]:
        return self._d["data"]

    @property
    def output(self) -> dict[str, Any]:
        return self._d["output"]

    @property
    def generation(self) -> dict[str, Any]:
        return self._d["generation"]

    @property
    def qc(self) -> dict[str, Any]:
        return self._d["qc"]

    # ---- 路径解析(相对 config.yaml 所在目录) ----
    def resolve(self, rel: str) -> Path:
        p = Path(rel)
        if p.is_absolute():
            return p
        return (self.base_dir / p).resolve()

    def defect_root(self) -> Path:
        return self.resolve(self.data["defect_root"])

    def clean_root(self) -> Path:
        return self.resolve(self.data["clean_root"])

    def out_path(self, key: str) -> Path:
        p = self.resolve(self.output[key])
        if key in ("catalog", "images", "masks", "root"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
        return p


def load_config(path: str | os.PathLike | None = None) -> Config:
    if path is None:
        path = PROJECT_ROOT / "config.yaml"
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # 允许用环境变量覆盖敏感项
    if os.getenv("RELAY_BASE_URL"):
        data["api"]["base_url"] = os.environ["RELAY_BASE_URL"]
    if os.getenv("RELAY_API_KEY"):
        data["api"]["api_key"] = os.environ["RELAY_API_KEY"]
    return Config(data, path)
