"""冻结感知的路径体系。

三个互不混淆的概念(见 design.md 第 4 节):

  install_root()   安装根目录。冻结时 = exe 所在目录; 源码时 = 仓库根。只读。
  resource_path()  打包资源。冻结时 = sys._MEIPASS; 源码时 = 仓库根。只读。
  workspace()      工作区。用户指定的可写目录, 放 config.yaml / outputs / logs / jobs。

为什么必须拆开: 安装目录通常在 Program Files 下, 普通用户不可写; 而本项目的
产物体积很大(debug 约 2GB, generated 可达数十 GB), 必须让用户选一块大容量磁盘。
另外 PyInstaller 冻结后 __file__ 指向解包临时目录, 沿用原来的
`Path(__file__).parent.parent` 会把产物写进临时目录然后被清掉。
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from . import _jsonstore

APP_DIR_NAME = "DefectSynth"

# 工作区来源(优先级从高到低): 进程内覆盖 -> 环境变量 -> 用户配置 -> 源码回退
WORKSPACE_ENV = "DEFECTSYNTH_WORKSPACE"

# 本文件位于 <repo>/app/runtime/paths.py, 因此仓库根是上溯两级
REPO_ROOT = Path(__file__).resolve().parents[2]

# 工作区内的标准子目录; 缺失时由 ensure_workspace_layout 创建
WORKSPACE_SUBDIRS = ("outputs", "logs", "jobs")

# 进程内覆盖。worker 子进程用 --workspace 显式传入, 不去改用户全局配置,
# 避免后台作业把用户在界面上的选择改掉。
_override: Path | None = None


class WorkspaceNotConfigured(RuntimeError):
    """冻结环境下尚未设置工作区。由界面捕获并引导用户选择目录。"""


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def install_root() -> Path:
    """安装根目录(只读)。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return REPO_ROOT


def resource_path(rel: str) -> Path:
    """打包进程序的只读资源, 如默认 config.yaml、QSS、图标。"""
    meipass = getattr(sys, "_MEIPASS", None)
    root = Path(meipass) if meipass else REPO_ROOT
    return root / rel


def user_config_dir() -> Path:
    """用户级配置目录(%APPDATA%\\DefectSynth)。"""
    appdata = os.getenv("APPDATA")
    base = Path(appdata) if appdata else Path.home() / "AppData" / "Roaming"
    p = base / APP_DIR_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def settings_file() -> Path:
    return user_config_dir() / "settings.json"


# --------------------------- 工作区 ---------------------------

def set_workspace_override(path: Path | str | None) -> None:
    """设置仅对当前进程有效的工作区, 不写入用户配置。"""
    global _override
    _override = Path(path).resolve() if path is not None else None


def _workspace_from_settings() -> Path | None:
    data = _jsonstore.read_json(settings_file(), default={})
    if isinstance(data, dict):
        raw = data.get("workspace")
        if isinstance(raw, str) and raw.strip():
            return Path(raw).expanduser()
    return None


def find_workspace() -> Path | None:
    """按优先级解析工作区; 未配置时返回 None 而不抛异常。"""
    if _override is not None:
        return _override
    env = os.getenv(WORKSPACE_ENV)
    if env and env.strip():
        return Path(env).expanduser().resolve()
    from_settings = _workspace_from_settings()
    if from_settings is not None:
        return from_settings.resolve()
    if not is_frozen():
        # 源码方式运行且未做任何配置时退回仓库根, 保证改造前后行为一致
        # (requirements.md 8.4 的向后兼容要求)
        return REPO_ROOT
    return None


def workspace() -> Path:
    """工作区目录。未配置且处于冻结环境时抛 WorkspaceNotConfigured。"""
    p = find_workspace()
    if p is None:
        raise WorkspaceNotConfigured(
            "尚未设置工作区。请在界面中选择一个可写目录用于存放"
            "配置、缺陷库与生成产物。")
    return p


def set_workspace(path: Path | str) -> Path:
    """持久化工作区到用户配置, 并准备好目录结构。"""
    p = Path(path).expanduser().resolve()
    p.mkdir(parents=True, exist_ok=True)
    if not is_writable(p):
        raise PermissionError(f"工作区目录不可写: {p}")
    _jsonstore.update_json(settings_file(), workspace=str(p))
    set_workspace_override(None)
    ensure_workspace_layout(p)
    return p


def is_writable(path: Path) -> bool:
    """真实写一个探针文件来判断可写性。

    不用 os.access: 它在 Windows 上对目录 ACL 的判断并不可靠, 常出现
    "报告可写但实际写入被拒"的情况。
    """
    probe = path / ".defectsynth_write_probe"
    try:
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except OSError:
        return False


def ensure_workspace_layout(path: Path | None = None) -> Path:
    """创建工作区标准子目录, 并在缺少 config.yaml 时从内置模板复制一份。

    复制而非内部读取, 是为了让用户仍能像改造前一样直接编辑 config.yaml 调参;
    同时 Config.resolve() 以 config.yaml 所在目录为基准, 把模板放进工作区
    可以让 outputs/ 等相对路径自然落在工作区内, 无需改动 config.py 的解析语义。
    """
    ws = Path(path) if path is not None else workspace()
    ws.mkdir(parents=True, exist_ok=True)
    for sub in WORKSPACE_SUBDIRS:
        (ws / sub).mkdir(parents=True, exist_ok=True)

    cfg = ws / "config.yaml"
    if not cfg.exists():
        template = resource_path("config.yaml")
        if template.exists() and template.resolve() != cfg.resolve():
            shutil.copyfile(template, cfg)
    return ws


def workspace_config_path() -> Path:
    """工作区内的 config.yaml 路径(必要时先初始化工作区结构)。"""
    ws = workspace()
    cfg = ws / "config.yaml"
    if not cfg.exists():
        ensure_workspace_layout(ws)
    return cfg


def logs_dir() -> Path:
    p = workspace() / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def jobs_dir() -> Path:
    p = workspace() / "jobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def catalog_dir() -> Path:
    """缺陷库目录。

    保持在 outputs/catalog 而不迁到 workspace/catalog: config.yaml 里
    output.catalog 的现值就是 "outputs/catalog", 不改路径即不改语义, 也保住了
    annotations.jsonl 中记录的路径字符串(断点续跑依赖它推导 stem)。
    """
    return workspace() / "outputs" / "catalog"


def disk_free_bytes(path: Path | str) -> int:
    """所在磁盘可用字节数; 用于界面在选择工作区时提示空间。"""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free
    except OSError:
        return 0
