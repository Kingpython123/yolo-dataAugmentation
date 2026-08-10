"""中转站连通性测试。

复用 CLI 的 test-api 两步验证(视觉模型 + 图像编辑模型), 但返回结构化结果而不是
打印。界面上必须明确提示这个操作会消耗少量额度 —— 它真的会各调一次大模型。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from app.runtime import secrets
from src.config import load_config


@dataclass
class StepResult:
    name: str
    ok: bool = False
    detail: str = ""
    elapsed: float = 0.0


@dataclass
class ApiTestResult:
    base_url: str = ""
    key_source: str = "none"
    steps: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def summary(self) -> str:
        if self.error:
            return self.error
        if not self.steps:
            return "未执行"
        failed = [s.name for s in self.steps if not s.ok]
        if failed:
            return f"{', '.join(failed)} 失败"
        return "视觉模型与图像编辑模型均可用"


def test_connectivity() -> ApiTestResult:
    """各调用一次视觉模型与图像编辑模型。会消耗少量额度。"""
    from PIL import Image

    result = ApiTestResult(key_source=secrets.key_source())
    if not secrets.has_api_key():
        result.error = "未设置 API key"
        return result

    try:
        cfg = load_config()
    except Exception as e:
        result.error = f"配置读取失败: {e}"
        return result

    try:
        from src.api_client import RelayClient
        relay = RelayClient(cfg)
    except Exception as e:
        # RelayClient 在 key 是占位符时会抛异常, 这里把它转成可读提示
        result.error = secrets.redact(f"客户端初始化失败: {e}")
        return result

    result.base_url = relay.base_url
    probe = Image.new("RGB", (64, 64), (120, 120, 120))

    step = StepResult(name="视觉模型")
    start = time.time()
    try:
        text = relay.chat_vision("用一句话描述这张图的主色。", images=[probe])
        step.ok = True
        step.detail = (text or "").strip()[:120]
    except Exception as e:
        step.detail = secrets.redact(str(e))[:300]
    step.elapsed = time.time() - start
    result.steps.append(step)

    step = StepResult(name="图像编辑模型")
    start = time.time()
    try:
        out = relay.edit_image(
            "Add a tiny realistic dark scratch in the center. "
            "Keep everything else identical.", probe)
        step.ok = True
        step.detail = f"返回图像 {out.size[0]}x{out.size[1]}"
    except Exception as e:
        step.detail = secrets.redact(str(e))[:300]
    step.elapsed = time.time() - start
    result.steps.append(step)

    return result
