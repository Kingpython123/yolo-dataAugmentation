"""褶皱形态描述子与相似检索(需求二方式 B)。

要解决的问题
------------
训练后发现某种褶皱识别效果差, 用户手上有一张"出问题的图"(误检区域截图, 或漏检
的真实褶皱照片), 想在缺陷库 674 张裁剪图里找出形态最像的若干条, 据此定向补充。

为什么基于 structure_ref 的浮雕图而不是原图
------------------------------------------
直接比原图会被三样东西主导, 而它们跟"褶皱形态"无关:
  1. 标签本身的印刷图案与文字(高频)
  2. 瓶身整体的光照渐变与明暗(低频)
  3. 源瓶自己的颜色
structure_ref.defect_structure_map() 做的正是带通滤波, 只留中频的褶皱起伏,
输出 128=平面、亮=凸起、暗=凹陷的灰度浮雕图。这一层已经在生成流程里被验证过
(它是喂给编辑模型的第三张参考图), 复用它比另造一套表示更稳妥。

描述子由四组特征拼成, 每组对应缺陷库里一个人能读懂的属性
------------------------------------------------------
  方向直方图      -> orientation(斜向/横向/竖向/交叉)
  多尺度带通能量  -> 粗细(宽大折痕 vs 细窄划痕)
  连通域统计      -> count(条数) 与 extent(覆盖范围)
  方向集中度      -> 平行成束 vs 多向杂乱

刻意不引入任何需要训练的模型: 674 张的规模不足以训练, 而且引入权重文件会破坏
"缺陷库可独立分发"的性质。

重要: 本模块的检索质量必须由 tools/check_morphology.py 的两道门槛验证后才可用
(自检 + 属性一致性), 不能凭"看起来合理"就接入生成命令。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from .config import Config
from .structure_ref import defect_structure_map

# 方向直方图: 0~180 度(梯度方向无正负之分, 一条褶皱的两侧梯度相反但同属一个走向)
ORIENT_BINS = 18

# 多尺度带通的 (sigma_small, sigma_large) 对。跨度覆盖"细线"到"宽大折痕":
# (1,4) 捕捉划痕级细线; (3,12) 中等褶皱; (8,30) 宽大折痕/鼓包
BANDPASS_SCALES = ((1.0, 4.0), (3.0, 12.0), (8.0, 30.0))

# 浮雕图偏离中性灰多少算"有起伏"。浮雕图以 128 为平面基准, defect_structure_map
# 的幅度上限是 min(127, 42*gain), 默认 gain=3 时约 ±126, 因此 18 大约相当于
# 峰值的 15%, 能滤掉噪声又不会把浅褶皱漏掉。
RELIEF_THRESH = 18

# 连通域面积下限(占整图比例), 低于此值视为噪点不计入条数
MIN_BLOB_AREA_RATIO = 0.0008

# 统一缩放到该尺寸再算描述子: 缺陷库裁剪图尺寸差异很大(短边下限 320, 长边可到
# 2000+), 不归一化的话"连通域数量""面积占比"这些量会被尺寸带偏。
DESCRIPTOR_SIZE = 256

CACHE_NAME = "morphology.npz"

# 各组特征在距离计算里的权重。方向与粗细是人眼判断"像不像"的主要依据, 因此给得
# 更高; 条数/覆盖范围受 VLM 标注与裁剪松紧影响较大, 权重压低避免噪声主导。
FEATURE_WEIGHTS = {
    "orientation": 2.0,
    "bandpass": 1.5,
    "blobs": 0.7,
    "concentration": 0.8,
}


@dataclass
class Descriptor:
    """一张裁剪图的形态描述子。各段分开存, 便于按组加权与调试。"""

    orientation: np.ndarray      # (ORIENT_BINS,) 归一化方向直方图
    bandpass: np.ndarray         # (len(BANDPASS_SCALES),) 归一化尺度能量
    blobs: np.ndarray            # (2,) [条数代理, 面积占比]
    concentration: np.ndarray    # (1,) 方向集中度

    def concat(self) -> np.ndarray:
        return np.concatenate([self.orientation, self.bandpass,
                               self.blobs, self.concentration])


@dataclass
class SimilarHit:
    entry_id: str
    distance: float
    record: dict = field(default_factory=dict)


def _prepare_relief(img: Image.Image) -> np.ndarray:
    """裁剪图 -> 归一化尺寸的浮雕图(float32, 以 0 为平面基准)。"""
    relief = defect_structure_map(img)
    gray = np.array(relief.convert("L"))
    gray = cv2.resize(gray, (DESCRIPTOR_SIZE, DESCRIPTOR_SIZE),
                      interpolation=cv2.INTER_AREA)
    # 平移到以 0 为基准, 便于后面用绝对值判断"起伏强度"
    return gray.astype(np.float32) - 128.0


def _orientation_histogram(relief: np.ndarray) -> tuple[np.ndarray, float]:
    """梯度方向直方图(按幅值加权) + 集中度。

    返回 (归一化直方图, 集中度)。集中度 = 1 - 归一化熵: 越接近 1 说明方向越集中
    (平行成束的褶皱), 越接近 0 说明方向越分散(多向交叉)。
    """
    gx = cv2.Sobel(relief, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(relief, cv2.CV_32F, 0, 1, ksize=3)
    magnitude = np.sqrt(gx * gx + gy * gy)
    # 取模 180 度: 梯度方向差 180 度属于同一走向
    angle = np.rad2deg(np.arctan2(gy, gx)) % 180.0

    hist, _ = np.histogram(angle, bins=ORIENT_BINS, range=(0.0, 180.0),
                           weights=magnitude)
    total = float(hist.sum())
    if total <= 1e-6:
        # 完全平坦的图(理论上不该出现在缺陷库里), 返回均匀分布且集中度 0
        return np.full(ORIENT_BINS, 1.0 / ORIENT_BINS, np.float32), 0.0
    hist = (hist / total).astype(np.float32)

    nonzero = hist[hist > 0]
    entropy = float(-(nonzero * np.log(nonzero)).sum())
    max_entropy = float(np.log(ORIENT_BINS))
    concentration = 1.0 - entropy / max_entropy if max_entropy > 0 else 0.0
    return hist, concentration


def _bandpass_energy(relief: np.ndarray) -> np.ndarray:
    """多尺度带通能量, 归一化成占比(反映起伏主要集中在哪个尺度上)。

    归一化是必要的: 不归一化时整体对比度高的图在所有尺度上都偏大, 距离会被
    "这张图整体多明显"主导, 而我们要比的是"粗细分布", 不是绝对强度。
    """
    energies = []
    for sigma_small, sigma_large in BANDPASS_SCALES:
        low = cv2.GaussianBlur(relief, (0, 0), sigma_small)
        high = cv2.GaussianBlur(relief, (0, 0), sigma_large)
        energies.append(float(np.abs(low - high).mean()))
    arr = np.asarray(energies, np.float32)
    total = float(arr.sum())
    if total <= 1e-6:
        return np.full(len(BANDPASS_SCALES), 1.0 / len(BANDPASS_SCALES),
                       np.float32)
    return arr / total


def _blob_stats(relief: np.ndarray) -> np.ndarray:
    """阈值化后的连通域统计: [条数代理, 面积占比]。

    条数用 log1p 压缩: 缺陷库里 count 从 1 到 25, 线性尺度下"20 条 vs 25 条"的
    差会和"1 条 vs 6 条"的差同等看待, 但后者在形态上的区别明显大得多。
    """
    mask = (np.abs(relief) > RELIEF_THRESH).astype(np.uint8)
    # 开运算去掉零星噪点, 避免把噪声数成褶皱条数
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

    total_px = float(mask.size)
    area_ratio = float(mask.sum()) / total_px

    n_labels, _labels, stats, _centroids = cv2.connectedComponentsWithStats(
        mask, connectivity=8)
    min_area = MIN_BLOB_AREA_RATIO * total_px
    # 标签 0 是背景, 跳过
    blob_count = sum(1 for i in range(1, n_labels)
                     if stats[i, cv2.CC_STAT_AREA] >= min_area)
    return np.asarray([np.log1p(blob_count), area_ratio], np.float32)


def describe(img: Image.Image) -> Descriptor:
    """计算一张裁剪图的形态描述子。"""
    relief = _prepare_relief(img)
    orient_hist, concentration = _orientation_histogram(relief)
    return Descriptor(
        orientation=orient_hist,
        bandpass=_bandpass_energy(relief),
        blobs=_blob_stats(relief),
        concentration=np.asarray([concentration], np.float32),
    )


def describe_path(path: Path | str) -> Descriptor | None:
    try:
        with Image.open(path) as im:
            return describe(im.convert("RGB"))
    except (OSError, ValueError):
        return None


def _weighted_distance(a: Descriptor, b: Descriptor) -> float:
    """按组加权的距离。

    方向直方图用 L1(直方图之间的自然度量, 对分布形状的差异更敏感且不会被单个
    bin 的极值放大); 其余用 L2。
    """
    w = FEATURE_WEIGHTS
    d_orient = float(np.abs(a.orientation - b.orientation).sum())
    d_band = float(np.linalg.norm(a.bandpass - b.bandpass))
    d_blob = float(np.linalg.norm(a.blobs - b.blobs))
    d_conc = float(np.abs(a.concentration - b.concentration).sum())
    return (w["orientation"] * d_orient + w["bandpass"] * d_band
            + w["blobs"] * d_blob + w["concentration"] * d_conc)


# --------------------------- 缓存 ---------------------------

def cache_path(cfg: Config) -> Path:
    return cfg.out_path("catalog") / CACHE_NAME


def build_cache(cfg: Config, records: list[dict],
                progress: bool = False) -> dict[str, Descriptor]:
    """为缺陷库每条目算一次描述子并缓存。674 张约需十几秒, 之后检索是毫秒级。"""
    descs: dict[str, Descriptor] = {}
    total = len(records)
    for i, rec in enumerate(records, 1):
        crop = cfg.resolve(str(rec.get("crop_path", "")))
        d = describe_path(crop)
        if d is not None:
            descs[rec["entry_id"]] = d
        if progress and (i % 100 == 0 or i == total):
            print(f"  形态描述子 {i}/{total}")

    if descs:
        ids = list(descs.keys())
        np.savez_compressed(
            cache_path(cfg),
            entry_ids=np.asarray(ids, dtype=object),
            orientation=np.stack([descs[i].orientation for i in ids]),
            bandpass=np.stack([descs[i].bandpass for i in ids]),
            blobs=np.stack([descs[i].blobs for i in ids]),
            concentration=np.stack([descs[i].concentration for i in ids]),
            meta=json.dumps({
                "orient_bins": ORIENT_BINS,
                "bandpass_scales": BANDPASS_SCALES,
                "relief_thresh": RELIEF_THRESH,
                "descriptor_size": DESCRIPTOR_SIZE,
            }),
        )
    return descs


def load_cache(cfg: Config) -> dict[str, Descriptor] | None:
    """读缓存; 不存在或参数与当前代码不一致时返回 None(交由调用方重建)。

    校验 meta 是必要的: 调过 ORIENT_BINS 之类的常数后, 旧缓存里的向量维度或
    语义已经变了, 混用会让检索结果悄悄失真而不报错。
    """
    path = cache_path(cfg)
    if not path.exists():
        return None
    try:
        data = np.load(path, allow_pickle=True)
        meta = json.loads(str(data["meta"]))
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None

    expected = {
        "orient_bins": ORIENT_BINS,
        "bandpass_scales": [list(s) for s in BANDPASS_SCALES],
        "relief_thresh": RELIEF_THRESH,
        "descriptor_size": DESCRIPTOR_SIZE,
    }
    actual = dict(meta)
    if "bandpass_scales" in actual:
        actual["bandpass_scales"] = [list(s) for s in actual["bandpass_scales"]]
    if actual != expected:
        return None

    ids = [str(x) for x in data["entry_ids"]]
    out: dict[str, Descriptor] = {}
    for i, eid in enumerate(ids):
        out[eid] = Descriptor(
            orientation=data["orientation"][i],
            bandpass=data["bandpass"][i],
            blobs=data["blobs"][i],
            concentration=data["concentration"][i],
        )
    return out


def get_descriptors(cfg: Config, records: list[dict],
                    rebuild: bool = False,
                    progress: bool = False) -> dict[str, Descriptor]:
    """取缺陷库描述子, 缓存可用则用缓存, 否则重建。

    还会检查缓存是否覆盖了当前缺陷库的全部条目 —— 缺陷库增量扩容后旧缓存会缺
    新条目, 那些条目在检索里就永远不会被召回, 属于沉默失效。
    """
    if not rebuild:
        cached = load_cache(cfg)
        if cached is not None:
            wanted = {r["entry_id"] for r in records}
            if wanted.issubset(cached.keys()):
                return {k: v for k, v in cached.items() if k in wanted}
    return build_cache(cfg, records, progress=progress)


# --------------------------- 检索 ---------------------------

def query_by_descriptor(descs: dict[str, Descriptor], query: Descriptor,
                        records_by_id: dict[str, dict],
                        top_k: int = 20,
                        exclude: set | None = None) -> list[SimilarHit]:
    exclude = exclude or set()
    hits = [
        SimilarHit(entry_id=eid, distance=_weighted_distance(query, d),
                   record=records_by_id.get(eid, {}))
        for eid, d in descs.items() if eid not in exclude
    ]
    hits.sort(key=lambda h: h.distance)
    return hits[:top_k]


def query_by_image(cfg: Config, image_path: Path | str,
                   records: list[dict], top_k: int = 20,
                   rebuild_cache: bool = False,
                   progress: bool = False) -> list[SimilarHit]:
    """给一张"出问题的图", 找形态最像的缺陷库条目。"""
    query = describe_path(image_path)
    if query is None:
        raise ValueError(f"无法读取图片: {image_path}")
    descs = get_descriptors(cfg, records, rebuild=rebuild_cache,
                            progress=progress)
    by_id = {r["entry_id"]: r for r in records}
    return query_by_descriptor(descs, query, by_id, top_k=top_k)


def query_by_entry(cfg: Config, entry_id: str, records: list[dict],
                   top_k: int = 20, include_self: bool = False,
                   rebuild_cache: bool = False) -> list[SimilarHit]:
    """以缺陷库里某条目为种子找相似条目(方式 C 的延伸: 扩大形态覆盖)。"""
    descs = get_descriptors(cfg, records, rebuild=rebuild_cache)
    if entry_id not in descs:
        raise ValueError(f"缺陷库描述子里没有该条目(裁剪图可能缺失): {entry_id}")
    by_id = {r["entry_id"]: r for r in records}
    exclude = set() if include_self else {entry_id}
    return query_by_descriptor(descs, descs[entry_id], by_id, top_k=top_k,
                               exclude=exclude)
