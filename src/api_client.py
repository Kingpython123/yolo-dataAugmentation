"""OpenAI 兼容的中转站客户端。

提供两类能力:
  1. chat_vision(): 多模态对话(读图 -> 文本/JSON), 用于缺陷编目、定位、质检。
  2. edit_image(): 参考引导的图像编辑(在裁块上加缺陷), 支持两种后端:
       - gemini_chat : 走 /chat/completions, 多图输入, 从返回中解析生成图
       - openai_images: 走 /images/edits, 支持显式二值 mask
"""
from __future__ import annotations

import base64
import io
import json
import re
import time
from typing import Any

import requests
from PIL import Image


# --------------------------- 图像 <-> base64 ---------------------------

def pil_to_data_url(img: Image.Image, fmt: str = "PNG") -> str:
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    mime = "image/png" if fmt.upper() == "PNG" else "image/jpeg"
    return f"data:{mime};base64,{b64}"


def pil_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _b64_to_pil(b64: str) -> Image.Image:
    raw = base64.b64decode(b64)
    return Image.open(io.BytesIO(raw)).convert("RGB")


_DATA_URL_RE = re.compile(r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\s]+)")


class APIError(RuntimeError):
    pass


class RelayClient:
    def __init__(self, cfg):
        api = cfg.api
        self.base_url = api["base_url"].rstrip("/")
        self.api_key = api["api_key"]
        self.timeout = api.get("timeout", 300)
        self.http_retries = api.get("http_retries", 2)
        self.models = cfg.models
        if not self.api_key or "REPLACE_ME" in self.api_key:
            raise APIError("请先在 config.yaml 的 api.api_key 填入中转站的 key")

    # --------------------------- 底层 HTTP ---------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _post_json(self, path: str, payload: dict, retries: int | None = None) -> dict:
        url = f"{self.base_url}{path}"
        retries = self.http_retries if retries is None else retries
        last = None
        for attempt in range(retries + 1):
            try:
                r = requests.post(url, headers=self._headers(),
                                  data=json.dumps(payload), timeout=self.timeout)
                if r.status_code >= 400:
                    raise APIError(f"HTTP {r.status_code}: {r.text[:500]}")
                return r.json()
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e  # 超时/连接问题: 指数退避后重试
                if attempt < retries:
                    time.sleep(min(30, 5 * (2 ** attempt)))
            except (requests.RequestException, APIError) as e:
                last = e
                if attempt < retries:
                    time.sleep(2 * (attempt + 1))
        raise APIError(f"请求失败 {url}: {last}")

    def _post_multipart(self, url: str, headers: dict, files, data: dict):
        """multipart 上传, 对超时/连接错误做指数退避重试。"""
        last = None
        for attempt in range(self.http_retries + 1):
            try:
                return requests.post(url, headers=headers, files=files,
                                     data=data, timeout=self.timeout)
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                if attempt < self.http_retries:
                    time.sleep(min(30, 5 * (2 ** attempt)))
            except requests.RequestException as e:
                last = e
                break
        raise APIError(f"multipart 请求失败 {url}: {last}")

    # --------------------------- 多模态对话 ---------------------------

    def chat_vision(self, prompt: str, images: list[Image.Image] | None = None,
                    model: str | None = None, temperature: float = 0.2,
                    expect_json: bool = False) -> str:
        model = model or self.models["vision"]
        content: list[dict] = [{"type": "text", "text": prompt}]
        for img in images or []:
            content.append({
                "type": "image_url",
                "image_url": {"url": pil_to_data_url(img, "JPEG")},
            })
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "temperature": temperature,
        }
        if expect_json:
            payload["response_format"] = {"type": "json_object"}
        try:
            resp = self._post_json("/chat/completions", payload)
        except APIError:
            if expect_json:
                payload.pop("response_format", None)
                resp = self._post_json("/chat/completions", payload)
            else:
                raise
        return _extract_text(resp)

    # --------------------------- 图像编辑 ---------------------------

    def edit_image(self, prompt: str, base_image: Image.Image,
                   references: list[Image.Image] | None = None,
                   mask: Image.Image | None = None,
                   model: str | None = None) -> Image.Image:
        backend = self.models.get("edit_backend", "gemini_chat")
        model = model or self.models["edit"]
        if backend == "openai_images":
            return self._edit_openai_images(prompt, base_image, references,
                                            mask, model)
        return self._edit_gemini_chat(prompt, base_image, references, model)

    def _edit_gemini_chat(self, prompt: str, base_image: Image.Image,
                          references: list[Image.Image] | None,
                          model: str) -> Image.Image:
        """走 /chat/completions, 首图=待编辑目标, 其余=参考缺陷图。"""
        content: list[dict] = [{"type": "text", "text": prompt}]
        content.append({"type": "image_url",
                        "image_url": {"url": pil_to_data_url(base_image, "PNG")}})
        for ref in references or []:
            content.append({"type": "image_url",
                            "image_url": {"url": pil_to_data_url(ref, "PNG")}})
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": content}],
            "modalities": ["image", "text"],  # 部分中转站需要显式声明
        }
        resp = self._post_json("/chat/completions", payload)
        img = _extract_image(resp)
        if img is None:
            raise APIError("编辑模型未返回图像, 原始响应片段: "
                          + json.dumps(resp)[:400])
        return img

    def _edit_openai_images(self, prompt: str, base_image: Image.Image,
                            references: list[Image.Image] | None,
                            mask: Image.Image | None, model: str) -> Image.Image:
        """走 /images/edits (multipart)。

        gpt-image-1/2 支持多张输入图: 第一张为待编辑目标, 其余为参考(最多8张);
        mask 作用于第一张(黑/透明区域为待重绘区)。
        size 用 auto, 由模型自定输出尺寸, 后续回贴时统一缩放对齐。
        """
        url = f"{self.base_url}/images/edits"
        # multipart 中用重复字段名 image[] 传多张图
        files: list[tuple[str, tuple[str, bytes, str]]] = [
            ("image[]", ("target.png", pil_to_png_bytes(base_image), "image/png")),
        ]
        for i, ref in enumerate(references or []):
            files.append(("image[]",
                          (f"ref{i}.png", pil_to_png_bytes(ref), "image/png")))
        if mask is not None:
            files.append(("mask",
                          ("mask.png", pil_to_png_bytes(mask), "image/png")))
        data = {"model": model, "prompt": prompt, "n": "1", "size": "auto"}
        headers = {"Authorization": f"Bearer {self.api_key}"}
        r = self._post_multipart(url, headers, files, data)
        if r.status_code >= 400:
            # 有参考图时禁止回退到单图形式: 单数 image 字段装不下参考图, 一旦降级
            # 模型就只能凭 prompt 文字自由发挥, 而 prompt 还在讲"image 2 是参考照片",
            # 产出的东西无法归因。直接报错, 交由上层重试。
            if references:
                raise APIError(
                    f"images/edits HTTP {r.status_code} (带 {len(references)} 张参考图): "
                    f"{r.text[:400]}")
            # 无参考图: 回退到单数 image 字段(部分实现只认这种, 且不支持 size=auto)
            files_single = [
                ("image", ("target.png", pil_to_png_bytes(base_image), "image/png")),
            ]
            if mask is not None:
                files_single.append(
                    ("mask", ("mask.png", pil_to_png_bytes(mask), "image/png")))
            data.pop("size", None)
            r = self._post_multipart(url, headers, files_single, data)
            if r.status_code >= 400:
                raise APIError(f"images/edits HTTP {r.status_code}: {r.text[:500]}")
        resp = r.json()
        img = _extract_image(resp)
        if img is None:
            raise APIError("images/edits 未返回图像: " + json.dumps(resp)[:400])
        return img


# --------------------------- 响应解析 ---------------------------

def _extract_text(resp: dict) -> str:
    try:
        msg = resp["choices"][0]["message"]
        c = msg.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):  # 部分实现返回分段
            return "".join(seg.get("text", "") for seg in c
                           if isinstance(seg, dict))
    except (KeyError, IndexError, TypeError):
        pass
    return json.dumps(resp)


def _extract_image(resp: dict) -> Image.Image | None:
    """尽量兼容多种中转站返回格式, 逐一尝试解析出一张图。"""
    # 1) OpenAI images 风格: data[].b64_json / url
    data = resp.get("data")
    if isinstance(data, list) and data:
        item = data[0]
        if item.get("b64_json"):
            return _b64_to_pil(item["b64_json"])
        if item.get("url"):
            return _download_image(item["url"])

    # 2) chat 风格
    choices = resp.get("choices")
    if isinstance(choices, list) and choices:
        msg = choices[0].get("message", {})
        # 2a) message.images: [{image_url:{url:...}}] 或 [{b64_json:...}]
        for img in msg.get("images", []) or []:
            if isinstance(img, dict):
                iu = img.get("image_url")
                if isinstance(iu, dict) and iu.get("url"):
                    got = _from_url_or_dataurl(iu["url"])
                    if got:
                        return got
                if img.get("b64_json"):
                    return _b64_to_pil(img["b64_json"])
        # 2b) content 为字符串, 内含 data URL 或 markdown 图
        content = msg.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for seg in content:
                if isinstance(seg, dict):
                    if seg.get("type") == "image_url":
                        iu = seg.get("image_url", {})
                        got = _from_url_or_dataurl(iu.get("url", ""))
                        if got:
                            return got
                    if seg.get("text"):
                        texts.append(seg["text"])
        for t in texts:
            got = _scan_text_for_image(t)
            if got:
                return got
    return None


def _from_url_or_dataurl(s: str) -> Image.Image | None:
    if not s:
        return None
    if s.startswith("data:image"):
        m = _DATA_URL_RE.search(s)
        if m:
            return _b64_to_pil(re.sub(r"\s", "", m.group(1)))
    if s.startswith("http"):
        return _download_image(s)
    return None


def _scan_text_for_image(text: str) -> Image.Image | None:
    m = _DATA_URL_RE.search(text)
    if m:
        return _b64_to_pil(re.sub(r"\s", "", m.group(1)))
    m = re.search(r"https?://\S+\.(?:png|jpg|jpeg|webp)", text)
    if m:
        try:
            return _download_image(m.group(0))
        except Exception:
            return None
    return None


def _download_image(url: str) -> Image.Image:
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")
