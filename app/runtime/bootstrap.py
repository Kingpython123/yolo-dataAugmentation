"""进程启动装配。

CLI、worker、GUI 三个入口第一件事都是调用本模块的 install()。它负责把应用层的
能力注入核心层声明的挂钩, 从而让核心层无需知道"工作区""凭据管理器"这些概念。

刻意做成显式调用而不是 import 副作用: 副作用式装配在冻结环境里很难排查, 而且
会让"源码直接跑 run.py"与"打包后跑"的行为出现难以察觉的分叉。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from src import config as core_config

from . import encoding, logging_setup, paths, secrets

_installed = False


def install(workspace: Path | str | None = None, *,
            log_name: str = "app.log",
            log_console: bool = False,
            log_level: int = logging.INFO,
            inject_api_key: bool = True) -> None:
    """装配运行环境。重复调用是安全的(除工作区覆盖外均为幂等)。

    workspace       显式指定工作区, 优先于用户配置。worker 子进程用它来跟随
                    父进程当时的选择, 而不是去读可能已被改动的全局配置。
    inject_api_key  把凭据管理器里的 key 放进本进程环境变量。
    """
    global _installed

    encoding.configure_stdio()

    if workspace is not None:
        paths.set_workspace_override(workspace)

    # 让核心层的 load_config(None) 指向工作区内的 config.yaml
    core_config.set_default_config_path_provider(_resolve_config_path)

    if inject_api_key:
        _inject_api_key()

    logging_setup.setup(filename=log_name, level=log_level, console=log_console)
    _installed = True


def is_installed() -> bool:
    return _installed


def _resolve_config_path() -> Path:
    """默认配置路径: 工作区内的 config.yaml。

    工作区未配置时(冻结环境首次启动)退回打包内置的模板, 使"读配置"这件事
    永远不会失败 —— 界面需要先能起来才能引导用户设置工作区。
    """
    ws = paths.find_workspace()
    if ws is None:
        return paths.resource_path("config.yaml")
    cfg = ws / "config.yaml"
    if not cfg.exists():
        paths.ensure_workspace_layout(ws)
    return cfg


def _inject_api_key() -> None:
    """把凭据管理器里的 key 注入本进程环境变量。

    这样做的理由:
      - 核心层已有 RELAY_API_KEY 环境变量通道, 复用它就不必改 config.py 的
        密钥逻辑, 也保住了用户原来手动设环境变量的习惯
      - 分离出去的 worker 进程能通过环境继承拿到 key, 无需把密钥落到任何文件

    已存在环境变量时不覆盖: 手动设置的优先级更高(见 secrets.get_api_key)。
    """
    if os.getenv(secrets.ENV_API_KEY):
        return
    try:
        key = secrets.get_api_key()
    except Exception:
        key = None
    if key:
        os.environ[secrets.ENV_API_KEY] = key
