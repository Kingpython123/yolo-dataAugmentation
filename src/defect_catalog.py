"""缺陷编目: 用视觉大模型分析所有类别的有缺陷样本, 构建可跨类检索的缺陷库。

产物:
  outputs/catalog/catalog.json      每条缺陷记录(类别/类型/描述/bbox/参考图)
  outputs/catalog/crops/*.png       缺陷参考裁剪图
"""
from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from pathlib import Path

from PIL import Image
from tqdm import tqdm

from .api_client import RelayClient
from .config import Config
from .dataset import scan_class_images

VISION_PROMPT = """你是工业质检视觉专家。这是一张有缺陷的瓶子照片。
我们【只关心两类缺陷】,请务必只标注这两类,其他一律忽略:
  1. 变形: 瓶身或标签的 褶皱/折痕/起皱/凹陷/鼓包/挤压变形/扭曲 都算变形。
  2. 划痕: 线状的 擦痕/刮痕/划伤。

【重要提示】部分瓶子(如"水立方/500水次方"透明高光标签瓶)的标签膜起皱、折痕非常常见且明显,
表现为标签表面成片的波浪状褶皱、斜向折痕、局部鼓起或塌陷。这属于"变形",
不要把这些明显的褶皱当成正常反光或高光而忽略!请优先、完整地把它们标注为变形。

忽略以下内容(不要标注): 污渍、斑点、异物、灰尘、水滴、正常反光、正常印刷图案、背景。

请输出严格的 JSON(不要多余文字),每个缺陷都要尽可能详细地描述,以便后续据此复现该缺陷:
{
  "has_defect": true/false,
  "defects": [
    {
      "type": "变形 或 划痕 (只能是这两个之一)",
      "location": "缺陷所在部位(如 瓶肩/瓶身上部/瓶身中部/瓶底)",
      "appearance": "整体外观概述(一到两句)",
      "severity": 1-5,
      "count": 褶皱条数或划痕条数(整数,估计值),
      "orientation": "主方向(如 横向 / 竖向 / 斜向左上-右下 / 多向交叉)",
      "geometry": "几何形态细节: 每条褶皱/划痕的长短、粗细、弯直、是否分叉或交叉、彼此如何排列(平行/放射/杂乱)",
      "extent": "覆盖范围: 大致占瓶身宽度或高度的比例, 跨越哪些部位",
      "photometry": "明暗表现: 是高光亮带还是暗色折痕, 是否明暗交替, 与周围表面的对比强弱",
      "edge_profile": "边缘特征: 锐利的折痕棱线 还是 平缓的隆起/凹陷过渡",
      "texture_effect": "对标签印刷图案/文字的影响: 是否被扭曲、拉伸、断裂或遮挡",
      "prompt_hint": "一句英文短句, 可直接用于指导图像生成模型复现该缺陷",
      "bbox": [x, y, w, h]
    }
  ]
}
bbox 为缺陷在图中的像素包围框,x,y 为左上角坐标,w,h 为宽高,坐标基于给你这张图的原始像素尺寸;
对"变形"请框住整片褶皱/折痕区域(尽量完整,不要只框一小块)。
severity: 1=极轻微几乎看不出, 3=明显, 5=非常夸张严重(大面积起皱/深折痕)。
若图中没有 变形 或 划痕,返回 {"has_defect": false, "defects": []}。"""

# 把模型可能给出的近义类型归一到白名单
_TYPE_ALIASES = {
    "变形": "变形", "褶皱": "变形", "折痕": "变形", "起皱": "变形",
    "凹陷": "变形", "鼓包": "变形", "挤压": "变形", "扭曲": "变形",
    "变形/褶皱": "变形", "标签起皱": "变形",
    "划痕": "划痕", "擦痕": "划痕", "刮痕": "划痕", "刮伤": "划痕",
    "划伤": "划痕", "线状划痕": "划痕",
}


def _normalize_type(raw: str, whitelist: list[str]) -> str | None:
    t = (raw or "").strip()
    mapped = _TYPE_ALIASES.get(t)
    if mapped is None:
        # 模糊匹配: 含关键字
        for k, v in _TYPE_ALIASES.items():
            if k in t:
                mapped = v
                break
    if mapped is None:
        return None
    return mapped if mapped in whitelist else None


@dataclass
class DefectEntry:
    entry_id: str
    class_name: str
    defect_type: str
    location: str
    appearance: str
    # --- 供生成模型参考的细化描述 ---
    severity: int
    count: int
    orientation: str
    geometry: str
    extent: str
    photometry: str
    edge_profile: str
    texture_effect: str
    prompt_hint: str
    # --- 溯源 ---
    source_image: str
    crop_path: str
    bbox: list[int]
    # 源图尺寸 [w, h]: bbox 是绝对像素, 而有缺陷/无缺陷样本分辨率并不一致,
    # 生成时需要据此把 bbox 换算成比例, 否则裁块尺寸会被隐性放大
    source_size: list[int]


def _safe_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"has_defect": False, "defects": []}


def _as_int(v, default: int, lo: int, hi: int) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _clamp_bbox(bbox, w, h):
    try:
        x, y, bw, bh = [int(round(float(v))) for v in bbox]
    except (ValueError, TypeError):
        return None
    x = max(0, min(x, w - 1))
    y = max(0, min(y, h - 1))
    bw = max(1, min(bw, w - x))
    bh = max(1, min(bh, h - y))
    if bw < 4 or bh < 4:
        return None
    return [x, y, bw, bh]


def _expand_crop(img: Image.Image, bbox, pad_ratio=0.35,
                 min_size: int = 320) -> Image.Image:
    """按比例外扩, 并保证裁剪最短边不小于 min_size(以缺陷为中心)。"""
    x, y, w, h = bbox
    cx, cy = x + w / 2.0, y + h / 2.0
    x0 = x - w * pad_ratio
    y0 = y - h * pad_ratio
    x1 = x + w + w * pad_ratio
    y1 = y + h + h * pad_ratio
    # 最短边下限
    if (x1 - x0) < min_size:
        x0, x1 = cx - min_size / 2, cx + min_size / 2
    if (y1 - y0) < min_size:
        y0, y1 = cy - min_size / 2, cy + min_size / 2
    # 越界回拉
    x0 = int(max(0, min(x0, img.width - 1)))
    y0 = int(max(0, min(y0, img.height - 1)))
    x1 = int(min(img.width, max(x1, x0 + 1)))
    y1 = int(min(img.height, max(y1, y0 + 1)))
    return img.crop((x0, y0, x1, y1))


def _clahe_enhance(img: Image.Image) -> Image.Image:
    """轻度CLAHE增强(仅提亮L通道对比度), 让微弱褶皱结构更清晰。"""
    import cv2
    import numpy as np
    bgr = cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    l = clahe.apply(l)
    out = cv2.cvtColor(cv2.merge([l, a, b]), cv2.COLOR_LAB2BGR)
    return Image.fromarray(cv2.cvtColor(out, cv2.COLOR_BGR2RGB))


def _process_image(client: Config, cfg: Config, class_name: str,
                   img_path: Path, crops_dir: Path):
    from .api_client import RelayClient  # 避免序列化问题
    relay: RelayClient = client
    cat_cfg = cfg.get("catalog", {}) or {}
    whitelist = cat_cfg.get("defect_types", ["变形", "划痕"])
    pad = cat_cfg.get("crop_pad_ratio", 0.35)
    pad_deform = cat_cfg.get("crop_pad_ratio_deform", 0.7)
    min_size = cat_cfg.get("crop_min_size", 320)
    enhance_ref = cat_cfg.get("enhance_reference", False)
    vlm_aid = cat_cfg.get("vlm_enhance_aid", False)

    try:
        img = Image.open(img_path).convert("RGB")
    except Exception:
        return []

    vlm_images = [img]
    prompt = VISION_PROMPT
    if vlm_aid:
        vlm_images = [img, _clahe_enhance(img)]
        prompt = VISION_PROMPT + (
            "\n\n注意: 我额外提供了第二张【对比增强图】(同一张图增强对比后),"
            "仅用于帮助你看清微弱的褶皱/划痕; 你的 bbox 坐标必须以第一张原图为准。")
    try:
        text = relay.chat_vision(prompt, images=vlm_images, expect_json=True)
    except Exception as e:
        print(f"[warn] VLM 失败 {img_path.name}: {e}")
        return []
    data = _safe_json(text)
    if not data.get("has_defect"):
        return []

    entries: list[DefectEntry] = []
    for i, d in enumerate(data.get("defects", [])):
        dtype = _normalize_type(d.get("type", ""), whitelist)
        if dtype is None:  # 非白名单类型(污渍/斑点/异物等)直接丢弃
            continue
        bbox = _clamp_bbox(d.get("bbox", []), img.width, img.height)
        if bbox is None:
            continue
        p = pad_deform if dtype == "变形" else pad
        crop = _expand_crop(img, bbox, pad_ratio=p, min_size=min_size)
        if enhance_ref:
            crop = _clahe_enhance(crop)
        eid = f"{class_name}__{img_path.stem}__{i}"
        # 文件名去掉可能的非法字符
        safe = eid.replace("（", "(").replace("）", ")")
        crop_path = crops_dir / f"{safe}.png"
        try:
            crop.save(crop_path)
        except Exception:
            crop_path = crops_dir / f"crop_{abs(hash(eid))}.png"
            crop.save(crop_path)
        def s(key: str) -> str:
            return str(d.get(key, "") or "").strip()

        # 路径统一存成相对 config.yaml 所在目录的形式, 保证缺陷库可跨机器分发
        def rel(p: Path) -> str:
            try:
                return Path(p).resolve().relative_to(cfg.base_dir).as_posix()
            except ValueError:
                return str(p)

        entries.append(DefectEntry(
            entry_id=eid,
            class_name=class_name,
            defect_type=dtype,
            location=s("location"),
            appearance=s("appearance"),
            severity=_as_int(d.get("severity"), 3, 1, 5),
            count=_as_int(d.get("count"), 1, 1, 999),
            orientation=s("orientation"),
            geometry=s("geometry"),
            extent=s("extent"),
            photometry=s("photometry"),
            edge_profile=s("edge_profile"),
            texture_effect=s("texture_effect"),
            prompt_hint=s("prompt_hint"),
            source_image=rel(img_path),
            crop_path=rel(crop_path),
            bbox=bbox,
            source_size=[img.width, img.height],
        ))
    return entries


def build_catalog(cfg: Config, max_per_class: int | None = None,
                  overwrite: bool = False,
                  only_classes: list[str] | None = None) -> list[dict]:
    relay = RelayClient(cfg)
    catalog_dir = cfg.out_path("catalog")
    crops_dir = catalog_dir / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    catalog_file = catalog_dir / "catalog.json"

    existing: list[dict] = []
    if catalog_file.exists():
        existing = json.loads(catalog_file.read_text(encoding="utf-8"))
        if not overwrite and not only_classes:
            print(f"[info] 已存在缺陷库 {catalog_file}, 使用 --overwrite 可重建")
            return existing

    class_images = scan_class_images(cfg, cfg.defect_root())
    if only_classes:
        class_images = {k: v for k, v in class_images.items() if k in only_classes}
        print(f"[info] 仅(重)建类别: {list(class_images.keys())}")
    rng = random.Random(cfg.generation.get("seed", 42))

    tasks = []
    for class_name, paths in class_images.items():
        if max_per_class and len(paths) > max_per_class:
            paths = rng.sample(paths, max_per_class)
        for p in paths:
            tasks.append((class_name, p))

    all_entries: list[DefectEntry] = []
    max_workers = cfg.api.get("max_workers", 3)
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_process_image, relay, cfg, cn, p, crops_dir): (cn, p)
                for cn, p in tasks}
        for fut in tqdm(as_completed(futs), total=len(futs), desc="编目缺陷"):
            try:
                all_entries.extend(fut.result())
            except Exception as e:
                print(f"[warn] 任务失败: {e}")

    records = [asdict(e) for e in all_entries]
    if only_classes and existing:
        # 仅重建指定类别: 保留其他类别的旧记录, 替换这些类别
        kept = [r for r in existing if r.get("class_name") not in class_images]
        records = kept + records
    catalog_file.write_text(json.dumps(records, ensure_ascii=False, indent=2),
                            encoding="utf-8")
    _print_summary(records)
    print(f"[done] 缺陷库已写入 {catalog_file} (共 {len(records)} 条)")
    return records


def _print_summary(records: list[dict]):
    from collections import Counter
    by_class = Counter(r["class_name"] for r in records)
    by_type = Counter(r["defect_type"] for r in records)
    by_sev = Counter(r.get("severity", "?") for r in records)
    print("\n=== 缺陷库统计 ===")
    print("按类别:", dict(by_class))
    print("按类型:", dict(by_type))
    print("按严重度:", dict(sorted(by_sev.items(), key=lambda x: str(x[0]))))


def load_catalog(cfg: Config) -> list[dict]:
    catalog_file = cfg.out_path("catalog") / "catalog.json"
    if not catalog_file.exists():
        raise FileNotFoundError(
            f"缺陷库不存在: {catalog_file}, 请先运行 build-catalog")
    return json.loads(catalog_file.read_text(encoding="utf-8"))
