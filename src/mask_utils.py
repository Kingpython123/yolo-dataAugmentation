"""瓶身分割、位置选择、缺陷差分掩膜、羽化回贴。

约束核心: 掩膜外像素与原图逐像素一致 -> 光场/大小/形状/背景绝不改动。
"""
from __future__ import annotations

import random
from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image


@dataclass
class Box:
    x: int
    y: int
    w: int
    h: int

    @property
    def x2(self) -> int:
        return self.x + self.w

    @property
    def y2(self) -> int:
        return self.y + self.h

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


def pil_to_bgr(img: Image.Image) -> np.ndarray:
    return cv2.cvtColor(np.array(img.convert("RGB")), cv2.COLOR_RGB2BGR)


def bgr_to_pil(arr: np.ndarray) -> Image.Image:
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


# --------------------------- 瓶身分割 ---------------------------

def segment_bottle(img: Image.Image,
                   roi: tuple[float, float, float, float] | list | None = None
                   ) -> np.ndarray:
    """返回瓶身前景的二值掩膜(uint8, 0/255)。

    两步:
      1. 暗背景剔除: 见 segment_bottle_flood(), 把与画面边界连通的暗区判为背景。
      2. ROI 限定(可选): 亮背景拍法(如 .bmp 类, 画面里有浅色桌布)无法靠亮度或
         几何把桌布与瓶身背光面分开 —— 实测两者都是贯穿全宽、贴左右边、填充率
         约 0.5 的大块。因固定工位下瓶子位置稳定, 改由 config 的
         data.bottle_roi 给出每类的矩形范围(比例坐标), 与前景求交。

    roi: (x0, y0, x1, y1) 比例坐标, 0~1; None 表示整幅画面。
    """
    mask = segment_bottle_flood(img)
    if roi is not None:
        mask = apply_roi(mask, roi)
    return mask


def apply_roi(mask: np.ndarray,
              roi: tuple[float, float, float, float] | list) -> np.ndarray:
    """把掩膜限制在比例坐标矩形 (x0, y0, x1, y1) 内。"""
    try:
        x0, y0, x1, y1 = [float(v) for v in roi]
    except (TypeError, ValueError):
        return mask
    h, w = mask.shape
    xa, xb = sorted((int(round(x0 * w)), int(round(x1 * w))))
    ya, yb = sorted((int(round(y0 * h)), int(round(y1 * h))))
    xa, ya = max(0, xa), max(0, ya)
    xb, yb = min(w, xb), min(h, yb)
    out = np.zeros_like(mask)
    if xb > xa and yb > ya:
        out[ya:yb, xa:xb] = mask[ya:yb, xa:xb]
    return out


def segment_bottle_otsu(img: Image.Image) -> np.ndarray:
    """旧版 Otsu 分割, 仅保留用于对比诊断(seg-check)。

    已知缺陷: 暗背景拍法上阈值落在镜面高光带, 1200 宽的图只分出 79~256 宽;
    亮背景拍法上"四角偏亮就取反"的逻辑会在类间翻转极性, 结果不一致。
    """
    bgr = pil_to_bgr(img)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.GaussianBlur(gray, (5, 5), 0)
    _, th = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    # 背景通常在四角; 若四角多为白, 则前景应取反
    h, w = th.shape
    corners = [th[0, 0], th[0, w - 1], th[h - 1, 0], th[h - 1, w - 1]]
    if np.mean(corners) > 127:
        th = 255 - th

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    th = cv2.morphologyEx(th, cv2.MORPH_CLOSE, kernel, iterations=2)
    th = cv2.morphologyEx(th, cv2.MORPH_OPEN, kernel, iterations=1)

    num, labels, stats, _ = cv2.connectedComponentsWithStats(th, connectivity=8)
    if num > 1:
        largest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        mask = np.where(labels == largest, 255, 0).astype(np.uint8)
    else:
        mask = th

    frac = float(mask.mean()) / 255.0
    if frac < 0.02 or frac > 0.98:  # 分割不可信 -> 中心竖条兜底
        mask = np.zeros((h, w), np.uint8)
        mask[int(h * 0.1):int(h * 0.9), int(w * 0.25):int(w * 0.75)] = 255
    return mask


def segment_bottle_flood(img: Image.Image, dark_delta: int = 12,
                         dark_cap: int = 70) -> np.ndarray:
    """按"背景 = 与画面边界连通的暗区域"分割瓶身(uint8, 0/255)。

    Otsu 版本在暗背景拍法上会失效: 阈值落在镜面高光上, 分出来的是一条高光
    带而不是瓶身(实测 1200 宽的图只分出 79~256 宽)。这里改用边界连通性:
      1. 从画面外框取背景灰度基准, 阈值 = 基准 + dark_delta(上限 dark_cap)
      2. 低于阈值且与边界连通的区域判为背景, 其余为前景
      3. 闭运算 + 保留最大连通域
    瓶身充满画面(边界不暗)时, 前景≈整幅画面 —— 此时本来就没有背景可排除。
    """
    g = cv2.GaussianBlur(np.array(img.convert("L")), (5, 5), 0)
    h, w = g.shape
    ring = np.concatenate([g[:3, :].ravel(), g[-3:, :].ravel(),
                          g[:, :3].ravel(), g[:, -3:].ravel()])
    thr = int(min(dark_cap, float(np.median(ring)) + dark_delta))
    dark = (g <= thr).astype(np.uint8)

    _, labels = cv2.connectedComponents(dark, connectivity=8)
    border = np.concatenate([labels[0, :], labels[-1, :],
                             labels[:, 0], labels[:, -1]])
    border_ids = [i for i in np.unique(border) if i != 0]
    bg = np.isin(labels, border_ids) if border_ids else np.zeros_like(dark, bool)
    fg = np.where(bg, 0, 255).astype(np.uint8)

    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (15, 15))
    fg = cv2.morphologyEx(fg, cv2.MORPH_CLOSE, k, iterations=2)
    num, lab, stats, _ = cv2.connectedComponentsWithStats(fg, connectivity=8)
    if num > 1:
        big = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        fg = np.where(lab == big, 255, 0).astype(np.uint8)

    frac = float(fg.mean()) / 255.0
    if frac < 0.02:  # 极端情况兜底
        fg = np.zeros((h, w), np.uint8)
        fg[int(h * 0.1):int(h * 0.9), int(w * 0.25):int(w * 0.75)] = 255
    return fg


# --------------------------- 位置选择 ---------------------------

def adaptive_patch_size(ref_bbox, base: int, max_size: int,
                        margin: float, img_size: tuple[int, int],
                        ref_img_size: tuple[int, int] | None = None
                        ) -> tuple[int, int]:
    """按参考缺陷 bbox 决定裁块尺寸(可为长方形), 保证大面积褶皱有足够画布。

    大缺陷若被塞进过小的裁块, 模型只能画缩小版 -> 表现为"缺陷不够夸张"。
    竖向贯穿式褶皱(如 165x2040)用正方形裁块无法覆盖, 因此按 bbox 长宽比
    分别计算宽高, 各自独立受 max_size 与图像尺寸约束。

    ref_img_size: 参考缺陷所在原图的尺寸。本项目里有缺陷样本与无缺陷样本
    分辨率并不一致(如 .bmp 类 796x1656 vs 675x1370, .jpg 类均为 1200x2400),
    直接拿绝对像素当裁块尺寸会带来最高约 1.8 倍的隐性放大, 使裁块越出瓶身
    落到桌布上。给出该参数时按"缺陷占原图比例"换算, 与分辨率无关。
    """
    iw, ih = img_size
    pw = ph = base
    if ref_bbox and len(ref_bbox) == 4:
        try:
            bw, bh = int(ref_bbox[2]), int(ref_bbox[3])
            if ref_img_size:
                rw, rh = int(ref_img_size[0]), int(ref_img_size[1])
                if rw > 0 and rh > 0:
                    bw = int(round(bw * iw / rw))
                    bh = int(round(bh * ih / rh))
            pw = max(base, int(bw * margin))
            ph = max(base, int(bh * margin))
        except (TypeError, ValueError, ZeroDivisionError):
            pw = ph = base
    pw = max(16, min(pw, max_size, iw))
    ph = max(16, min(ph, max_size, ih))
    return int(pw), int(ph)


def pick_patch_box(bottle_mask: np.ndarray, patch_size, rng: random.Random,
                   margin_ratio: float = 0.12,
                   min_bottle_cover: float = 0.55,
                   tries: int = 40) -> Box:
    """在瓶身掩膜内选一个裁块, 尽量避开边缘。

    patch_size 可为 int(正方形) 或 (宽, 高)。
    min_bottle_cover: 裁块内瓶身像素占比下限。大裁块容易越出瓶身落到桌布上,
    这里多次采样挑覆盖率最高的位置, 避免把缺陷画到背景/桌布。
    """
    h, w = bottle_mask.shape
    if isinstance(patch_size, (tuple, list)):
        pw, ph = int(patch_size[0]), int(patch_size[1])
    else:
        pw = ph = int(patch_size)
    pw, ph = min(pw, w), min(ph, h)

    # 腐蚀掉边缘, 使裁块中心尽量落在瓶身内部
    er = max(3, int(min(pw, ph) * margin_ratio))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (er, er))
    inner = cv2.erode(bottle_mask, kernel, iterations=1)
    ys, xs = np.where(inner > 0)
    if len(xs) == 0:
        ys, xs = np.where(bottle_mask > 0)
    if len(xs) == 0:
        return Box((w - pw) // 2, (h - ph) // 2, pw, ph)

    best: tuple[float, Box] | None = None
    for _ in range(max(1, tries)):
        idx = rng.randrange(len(xs))
        cx, cy = int(xs[idx]), int(ys[idx])
        x = int(np.clip(cx - pw // 2, 0, max(0, w - pw)))
        y = int(np.clip(cy - ph // 2, 0, max(0, h - ph)))
        cover = float((bottle_mask[y:y + ph, x:x + pw] > 0).mean())
        if best is None or cover > best[0]:
            best = (cover, Box(x, y, pw, ph))
        if cover >= min_bottle_cover:
            return best[1]
    return best[1]


def crop(img: Image.Image, box: Box) -> Image.Image:
    return img.crop((box.x, box.y, box.x2, box.y2))


# --------------------------- 光度对齐 ---------------------------

def align_photometry(edited_patch: Image.Image, orig_patch: Image.Image,
                     iters: int = 2, keep_pct: float = 80.0) -> Image.Image:
    """把 edited 的全局亮度/色偏对齐到 orig, 消除生成模型整图重渲染带来的漂移。

    对每个通道做鲁棒线性拟合 edited*g + b ≈ orig:
      1. 用均值/标准差初始化 g,b
      2. 迭代剔除残差大的像素(缺陷区属少数派离群点)后重新拟合
    这样只校正全局漂移, 不会把真实缺陷一起"拉平"。
    """
    o = pil_to_bgr(orig_patch).astype(np.float32)
    e = pil_to_bgr(edited_patch)
    if e.shape[:2] != o.shape[:2]:
        e = cv2.resize(e, (o.shape[1], o.shape[0]))
    e = e.astype(np.float32)

    out = np.empty_like(e)
    for c in range(3):
        ec, oc = e[..., c].ravel(), o[..., c].ravel()
        keep = np.ones_like(ec, dtype=bool)
        g, b = 1.0, 0.0
        for _ in range(max(1, iters)):
            ek, ok = ec[keep], oc[keep]
            if ek.size < 32:
                break
            se = float(ek.std())
            if se < 1e-3:
                g, b = 1.0, float(ok.mean() - ek.mean())
            else:
                g = float(ok.std()) / se
                g = float(np.clip(g, 0.5, 2.0))
                b = float(ok.mean() - g * ek.mean())
            resid = np.abs(ec * g + b - oc)
            cut = np.percentile(resid, keep_pct)
            keep = resid <= max(cut, 1e-6)
        out[..., c] = e[..., c] * g + b
    return bgr_to_pil(np.clip(out, 0, 255).astype(np.uint8))


# --------------------------- 缺陷差分掩膜 ---------------------------

def diff_defect_mask(orig_patch: Image.Image, edited_patch: Image.Image,
                     thresh: int = 18, min_area_ratio: float = 0.0006,
                     max_area_ratio: float = 0.85,
                     return_info: bool = False):
    """差分求缺陷掩膜(uint8 0/255)。

    步骤: 通道最大差 -> 阈值 -> 形态学 -> 按面积保留显著连通域。
    注意: 调用前应先用 align_photometry 对齐, 否则全局漂移会被误当作缺陷。

    关于面积上限(max_area_ratio):
      它的本意是拦"模型整图重渲染造成的全局漂移", 但面积是个很差的判据 ——
      真实的大面积褶皱同样会形成贯通裁块的大连通域。实测参考缺陷覆盖面积大时,
      阈值化后常出现单个占比 0.69~0.76 的连通域, 旧实现直接 continue 跳过,
      而它又是唯一的连通域, 于是掩膜整体归零, 表现为"模型压根没生成缺陷"
      (实际模型改动幅度很大)。
      因此这里改为: 超上限的域不再静默丢弃, 若过滤后掩膜为空, 则保留其中最大的
      那个域并在 info 里标记 fallback_used, 交由后续的瓶身/边界约束去裁剪、
      交由质检去判断贴回效果。宁可让质检看到再判, 也不要在这里悄悄丢掉。

    return_info=True 时返回 (mask, info), info 含各环节占比, 便于归因与落盘。
    """
    o = pil_to_bgr(orig_patch).astype(np.int16)
    e = pil_to_bgr(edited_patch).astype(np.int16)
    if o.shape != e.shape:
        e = cv2.resize(e.astype(np.uint8), (o.shape[1], o.shape[0])).astype(np.int16)

    diff = np.abs(e - o).max(axis=2).astype(np.uint8)
    diff = cv2.GaussianBlur(diff, (3, 3), 0)
    _, m = cv2.threshold(diff, thresh, 255, cv2.THRESH_BINARY)

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel, iterations=2)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel, iterations=1)

    total = m.shape[0] * m.shape[1]
    num, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = np.zeros_like(m)
    oversized: list[tuple[int, int]] = []   # (label, area)
    for i in range(1, num):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area_ratio * total:
            continue                        # 噪点级, 忽略
        if area > max_area_ratio * total:
            oversized.append((i, area))     # 先记下, 不直接丢
            continue
        out[labels == i] = 255

    info = {
        "diff_mean": round(float(diff.mean()), 2),
        "over_thresh_ratio": round(float((diff > thresh).mean()), 4),
        "prefilter_ratio": round(float((m > 0).mean()), 4),
        "oversized_count": len(oversized),
        "fallback_used": False,
    }
    if not out.any() and oversized:
        # 过滤后什么都不剩: 保留最大的超限域, 而不是产出空掩膜
        biggest = max(oversized, key=lambda t: t[1])[0]
        out[labels == biggest] = 255
        info["fallback_used"] = True
    info["mask_ratio"] = round(float((out > 0).mean()), 4)
    return (out, info) if return_info else out


def constrain_mask(patch_mask: np.ndarray, bottle_mask: np.ndarray, box: Box,
                   border_fade_ratio: float = 0.08,
                   bottle_erode: int = 6) -> np.ndarray:
    """对缺陷掩膜施加两项约束(只做限制, 不改画面内容)。

    1. 与瓶身掩膜求交: 缺陷不可能落到桌布/背景上。
    2. 裁块边界内缩: 掩膜在触及裁块边缘前必须归零, 否则贴回后裁块内外
       的标签图案接不上, 表现为"瓶身上下不一样"的断层。
    """
    m = patch_mask.copy()
    ph, pw = m.shape

    # --- 1) 只保留落在瓶身内部的部分 ---
    if bottle_mask is not None:
        bm = bottle_mask[box.y:box.y2, box.x:box.x2]
        if bm.shape != m.shape:
            bm = cv2.resize(bm, (pw, ph), interpolation=cv2.INTER_NEAREST)
        if bottle_erode > 0:  # 稍微内缩, 避开瓶身轮廓边缘
            k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE,
                                          (bottle_erode * 2 + 1,) * 2)
            bm = cv2.erode(bm, k, iterations=1)
        m = cv2.bitwise_and(m, bm)

    # --- 2) 裁块边界带内强制归零, 保证过渡落在裁块内部 ---
    bw = max(2, int(min(pw, ph) * border_fade_ratio))
    ramp = np.ones((ph, pw), np.float32)
    ramp[:bw, :] *= np.linspace(0, 1, bw, dtype=np.float32)[:, None]
    ramp[-bw:, :] *= np.linspace(1, 0, bw, dtype=np.float32)[:, None]
    ramp[:, :bw] *= np.linspace(0, 1, bw, dtype=np.float32)[None, :]
    ramp[:, -bw:] *= np.linspace(1, 0, bw, dtype=np.float32)[None, :]
    m = (m.astype(np.float32) * ramp).astype(np.uint8)
    return m


def binarize_mask(alpha: np.ndarray, thresh: int = 128) -> np.ndarray:
    """把用于融合的软 alpha 转成用于标注的二值掩膜。

    羽化/边界斜坡会产生大量低值过渡像素; 若直接用 >0 当标注,
    bbox 会被过渡带虚增(实测可达数十像素), 影响 YOLO 标签精度。
    """
    _, m = cv2.threshold(alpha, int(thresh), 255, cv2.THRESH_BINARY)
    return m


def mask_bbox(mask: np.ndarray, thresh: int = 128) -> Box | None:
    """缺陷包围框。按 thresh 二值化后再取框, 避免羽化过渡带虚增 bbox。"""
    ys, xs = np.where(mask >= thresh)
    if len(xs) == 0:
        ys, xs = np.where(mask > 0)
    if len(xs) == 0:
        return None
    return Box(int(xs.min()), int(ys.min()),
               int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1))


def changed_ratio(mask: np.ndarray) -> float:
    return float((mask > 0).mean())


# --------------------------- 羽化回贴(硬约束) ---------------------------

def feather_alpha(patch_mask: np.ndarray, feather: int = 10) -> np.ndarray:
    """对掩膜做羽化, 返回 0..255 的软 alpha。

    注意: 必须在 constrain_mask 之前调用。高斯模糊会把 alpha 向外扩散,
    若在约束之后羽化, 会把掩膜重新吹出瓶身/裁块边界, 打穿已施加的约束。
    """
    a = patch_mask.astype(np.float32)
    if feather > 0:
        k = int(feather) * 2 + 1
        a = cv2.GaussianBlur(a, (k, k), 0)
    return np.clip(a, 0, 255).astype(np.uint8)


def feather_composite(orig_full: Image.Image, edited_patch: Image.Image,
                      patch_mask: np.ndarray, box: Box,
                      feather: int = 10, gain: float = 1.0) -> Image.Image:
    """把 edited_patch 的缺陷(delta)羽化叠加回 orig_full 的 box 位置。

    result = 原图 + gain * alpha * (编辑图 - 原图)
    - 完整保留模型生成的褶皱强度(不像泊松会洗掉低频明暗)。
    - gain>1 可进一步加强褶皱; box 外与掩膜外像素保持原图。
    """
    base = np.array(orig_full.convert("RGB")).astype(np.float32)
    region = base[box.y:box.y2, box.x:box.x2].copy()

    ep = np.array(edited_patch.convert("RGB").resize((box.w, box.h))).astype(np.float32)

    # patch_mask 已是最终 alpha(羽化应在 constrain_mask 之前完成), 此处不再模糊
    alpha = patch_mask.astype(np.float32)
    if alpha.shape != (box.h, box.w):
        alpha = cv2.resize(alpha, (box.w, box.h))
    alpha = (alpha / 255.0)[..., None]

    if abs(gain - 1.0) < 1e-6:
        # gain=1: 掩膜内完全采用模型输出, 不做任何像素增强
        blended = region + alpha * (ep - region)
    else:
        # 软限幅: 增益后接近饱和的像素逐步压缩, 避免高光被削平丢失细节
        delta = gain * alpha * (ep - region)
        target = region + delta
        over = np.clip((target - 235.0) / 20.0, 0, 1)
        under = np.clip((20.0 - target) / 20.0, 0, 1)
        soften = 1.0 - np.maximum(over, under) * 0.6
        blended = region + delta * soften
    base[box.y:box.y2, box.x:box.x2] = blended
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


def seamless_composite(orig_full: Image.Image, edited_patch: Image.Image,
                       patch_mask: np.ndarray, box: Box,
                       mode: str = "mixed") -> Image.Image:
    """泊松无缝融合: 把 edited_patch 的缺陷区域按梯度域融合进原图 box 处。

    相比羽化叠加, 边缘/高光与周围光照衔接更自然, 无明显贴块痕迹。
    mode: normal(替换) | mixed(混合梯度, 更能保留原图高光结构)。
    仅掩膜内像素被改变, 掩膜外保持原图(硬约束不破坏)。
    """
    dst = pil_to_bgr(orig_full)
    src = pil_to_bgr(edited_patch.resize((box.w, box.h)))

    mask = patch_mask.copy()
    if mask.shape != (box.h, box.w):
        mask = cv2.resize(mask, (box.w, box.h))
    _, mask = cv2.threshold(mask, 8, 255, cv2.THRESH_BINARY)
    # 避免掩膜贴到 patch 边缘导致 seamlessClone 报错/溢出
    mask[:2, :] = 0
    mask[-2:, :] = 0
    mask[:, :2] = 0
    mask[:, -2:] = 0
    if mask.sum() == 0:
        return orig_full

    center = (box.x + box.w // 2, box.y + box.h // 2)
    flags = cv2.MIXED_CLONE if mode == "mixed" else cv2.NORMAL_CLONE
    try:
        out = cv2.seamlessClone(src, dst, mask, center, flags)
    except cv2.error:
        # 退化: 泊松失败时回退羽化叠加
        return feather_composite(orig_full, edited_patch, patch_mask, box)
    # 硬约束: 掩膜外强制还原为原图, 消除泊松可能的边界外溢
    full_mask = np.zeros(dst.shape[:2], np.uint8)
    full_mask[box.y:box.y2, box.x:box.x2] = mask
    m3 = (full_mask > 0)[..., None]
    out = np.where(m3, out, dst)
    return bgr_to_pil(out)


def composite(orig_full: Image.Image, edited_patch: Image.Image,
              patch_mask: np.ndarray, box: Box, mode: str = "feather",
              feather: int = 10, gain: float = 1.0) -> Image.Image:
    """按 mode 选择融合方式的统一入口。
    mode: feather(推荐,保留褶皱强度) | poisson | poisson_mixed(会洗淡低频褶皱,慎用)
    """
    if mode == "poisson":
        return seamless_composite(orig_full, edited_patch, patch_mask, box, "normal")
    if mode == "poisson_mixed":
        return seamless_composite(orig_full, edited_patch, patch_mask, box, "mixed")
    return feather_composite(orig_full, edited_patch, patch_mask, box, feather, gain)


def full_mask_from_patch(full_size: tuple[int, int], patch_mask: np.ndarray,
                         box: Box) -> Image.Image:
    """把 patch 内掩膜放回整图尺寸, 返回单通道 PIL 掩膜。"""
    w, h = full_size
    canvas = np.zeros((h, w), np.uint8)
    pm = patch_mask
    if pm.shape != (box.h, box.w):
        pm = cv2.resize(pm, (box.w, box.h))
    canvas[box.y:box.y2, box.x:box.x2] = pm
    return Image.fromarray(canvas)


def openai_edit_mask(patch_w: int, patch_h: int | None = None,
                     editable_ratio: float = 0.6) -> Image.Image:
    """为 /images/edits 生成 mask(RGBA), 支持长方形裁块。

    OpenAI 约定: alpha=0(透明)区域允许重绘, alpha=255(不透明)区域保持。
    把裁块中心 editable_ratio 区域设为可编辑, 四周保持, 减少全局重渲染。
    """
    pw = int(patch_w)
    ph = int(patch_h) if patch_h else pw
    ew, eh = max(1, int(pw * editable_ratio)), max(1, int(ph * editable_ratio))
    ox, oy = (pw - ew) // 2, (ph - eh) // 2
    alpha = np.full((ph, pw), 255, np.uint8)
    alpha[oy:oy + eh, ox:ox + ew] = 0  # 中心可编辑
    k = max(3, (min(pw, ph) // 25) * 2 + 1)  # 羽化核随尺寸自适应
    alpha = cv2.GaussianBlur(alpha, (k, k), 0)
    rgba = np.zeros((ph, pw, 4), np.uint8)
    rgba[..., 3] = alpha
    return Image.fromarray(rgba, mode="RGBA")


def background_change_ratio(orig_full: Image.Image, result_full: Image.Image,
                            defect_mask: np.ndarray, pixel_thresh: int = 12) -> float:
    """校验掩膜外变化像素比例(硬约束验证, 理论上应≈0)。"""
    o = np.array(orig_full.convert("RGB")).astype(np.int16)
    r = np.array(result_full.convert("RGB")).astype(np.int16)
    diff = np.abs(o - r).max(axis=2)
    changed = diff > pixel_thresh
    outside = defect_mask == 0
    denom = int(outside.sum())
    if denom == 0:
        return 0.0
    return float((changed & outside).sum()) / denom
