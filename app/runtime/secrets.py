"""API key 的安全存取。

设计约束(requirements.md FR-3.2 / NFR-2):
  - 绝不写入 config.yaml(该文件在 git 里, 写进去等于泄露)
  - 绝不写入 settings.json(明文文件)
  - 绝不嵌入可执行文件(exe 里的明文字符串可被直接提取)

实现: 交给 Windows 凭据管理器(通过 keyring), 由操作系统按当前用户加密保管。
读取顺序保留环境变量优先, 这样原有的 `$env:RELAY_API_KEY="sk-..."` 习惯与
自动化脚本不受影响(FR-3.4)。
"""
from __future__ import annotations

import os
import re

SERVICE_NAME = "DefectSynth"
KEY_ENTRY = "relay_api_key"

ENV_API_KEY = "RELAY_API_KEY"
ENV_BASE_URL = "RELAY_BASE_URL"

# config.yaml 里的占位符, 等同于"没填"
PLACEHOLDER_MARK = "REPLACE_ME"

# 日志/界面脱敏用: 匹配 sk- 开头的长串
_SECRET_RE = re.compile(r"(sk-[A-Za-z0-9]{2})[A-Za-z0-9_\-]{6,}")


class KeyringUnavailable(RuntimeError):
    """凭据管理器不可用(例如非 Windows 或后端缺失)。"""


def _keyring():
    try:
        import keyring
    except ImportError as e:  # pragma: no cover - 依赖缺失时的显式提示
        raise KeyringUnavailable(
            "缺少 keyring 依赖, 无法读写系统凭据管理器。"
            "可改用环境变量 RELAY_API_KEY。") from e
    return keyring


# --------------------------- 读写 ---------------------------

def get_api_key() -> str | None:
    """按 环境变量 -> 凭据管理器 的顺序取 key; 都没有时返回 None。"""
    env = os.getenv(ENV_API_KEY)
    if env and env.strip() and PLACEHOLDER_MARK not in env:
        return env.strip()
    try:
        stored = _keyring().get_password(SERVICE_NAME, KEY_ENTRY)
    except Exception:
        # 凭据管理器不可用不应让程序无法启动: 上层会提示用户去设置页填写
        return None
    if stored and stored.strip():
        return stored.strip()
    return None


def set_api_key(key: str) -> None:
    key = (key or "").strip()
    if not key:
        raise ValueError("API key 不能为空")
    if PLACEHOLDER_MARK in key:
        raise ValueError("这看起来是 config.yaml 里的占位符, 不是真实 key")
    _keyring().set_password(SERVICE_NAME, KEY_ENTRY, key)


def clear_api_key() -> None:
    try:
        _keyring().delete_password(SERVICE_NAME, KEY_ENTRY)
    except Exception:
        # 本来就没存过时 keyring 会抛异常, 这里视作已清除
        pass


def has_api_key() -> bool:
    return get_api_key() is not None


def key_source() -> str:
    """返回当前 key 的来源, 供界面显示: env | credential_manager | none。"""
    env = os.getenv(ENV_API_KEY)
    if env and env.strip() and PLACEHOLDER_MARK not in env:
        return "env"
    try:
        if _keyring().get_password(SERVICE_NAME, KEY_ENTRY):
            return "credential_manager"
    except Exception:
        pass
    return "none"


def get_base_url() -> str | None:
    """base_url 不是机密, 但环境变量通道要与 key 保持一致的语义。"""
    env = os.getenv(ENV_BASE_URL)
    return env.strip() if env and env.strip() else None


# --------------------------- 脱敏 ---------------------------

def mask(key: str | None) -> str:
    """界面展示用: 只保留前缀与后 4 位。"""
    if not key:
        return "(未设置)"
    key = key.strip()
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:3]}****{key[-4:]}"


def redact(text: str) -> str:
    """把任意文本里的 key 形态串替换掉, 供日志与错误信息使用。

    中转站返回的错误里有时会回显请求头, 因此不能只在自己打日志的地方脱敏。
    """
    if not text:
        return text
    return _SECRET_RE.sub(r"\1***", text)
