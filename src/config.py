"""配置加载与路径解析。

关于默认 config.yaml 的位置
--------------------------
原实现固定取 `PROJECT_ROOT / "config.yaml"`, 其中 PROJECT_ROOT 由 __file__
上溯两级得到。这在 PyInstaller 冻结后会指向解包临时目录, 导致配置读不到、
产物写进临时目录随后被清掉。

修正方式是依赖倒置而不是让核心层去感知冻结环境: 本模块只声明一个"默认配置
路径由谁决定"的挂钩, 由应用层(app/runtime/bootstrap.py)注入工作区解析逻辑。
未注入时行为与改造前完全一致 —— 这保证了源码方式直接跑 run.py 的既有用法
不受影响, 也维持了 app -> src 的单向依赖。

Config.resolve() 的语义刻意保持不变(相对 config.yaml 所在目录解析)。因为
工作区里就放着 config.yaml, 所以 output.* 那些相对路径会自然落在工作区内,
不需要改动任何解析逻辑, annotations.jsonl 里记录的路径语义也就不会变
(断点续跑依赖它推导 stem)。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 应用层注入的默认配置路径解析器; None 表示沿用改造前的行为
_default_config_path_provider: Callable[[], Path] | None = None


def set_default_config_path_provider(
        provider: Callable[[], Path] | None) -> Callable[[], Path] | None:
    """注入"默认 config.yaml 在哪"的解析逻辑, 返回被替换掉的那个。"""
    global _default_config_path_provider
    previous = _default_config_path_provider
    _default_config_path_provider = provider
    return previous


def default_config_path() -> Path:
    """load_config(path=None) 时使用的配置文件路径。"""
    if _default_config_path_provider is not None:
        return Path(_default_config_path_provider())
    return PROJECT_ROOT / "config.yaml"


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
        path = default_config_path()
    path = Path(path)
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # 允许用环境变量覆盖敏感项
    if os.getenv("RELAY_BASE_URL"):
        data["api"]["base_url"] = os.environ["RELAY_BASE_URL"]
    if os.getenv("RELAY_API_KEY"):
        data["api"]["api_key"] = os.environ["RELAY_API_KEY"]
    return Config(data, path)
