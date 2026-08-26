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


# ------------------- 平坦底图编辑 + 乘性贴回(flat-edit) -------------------
# 默认值集中在此处定义, 作为唯一来源。
# 之前把默认值同时写在"函数签名"和"generate.py 的 gen.get() 兜底值"两处,
# 结果调膝点时只改了签名和 config、漏了兜底值 -> config 里该 key 一旦缺失或
# 拼错就会静默回退到一个实测更差的旧值(235, 亮部次序 0.895 vs 252 的 0.932),
# 而且不报错、不易发现。函数签名与调用处都引用这里的常量, 从根上消除这类不一致。
FLAT_SIGMA_RATIO = 0.022
FLAT_SIGMA_MIN = 12.0
FLAT_SIGMA_MAX = 32.0
RATIO_EPS_RATIO = 0.05
RATIO_EPS_FLOOR = 2.0
RATIO_LOG_CLAMP_LO = -1.10
RATIO_LOG_CLAMP_HI = 0.40
RATIO_HIGHLIGHT_KNEE = 252.0
RATIO_MASK_THRESH = 0.06
# --- 真实明暗场直接传递(relief transfer) ---
# 编辑模型无法把参考的几何映射到目标坐标系: 实测参考主走向与生成主走向的角度差
# 均值 68 度(纯随机的期望是 45 度), 走向吻合率仅 14%; 补上最强的"1:1复刻 + 禁止
# 等距平行"指令后吻合率仍是 14%, 精确的零改善。同时模型自身高度自洽(同一参考在
# 不同干净图上产出走向的圆集中度 0.96~1.00), 说明它稳定地画自己的先验, 与参考无关。
# 因此几何不再交给模型发明, 直接搬运真实缺陷的明暗调制场。
RELIEF_SIGMA_SMALL = 10.0   # 高频截断: 压掉印刷笔画(直接传递时残留会变成真实阴影)
RELIEF_SIGMA_LARGE = 40.0   # 低频截断: 压掉源瓶整体光照
RELIEF_AMPLITUDE = 1.0      # 幅度倍率; 1.0 = 忠实还原真实幅度
RELIEF_EPS = 12.0           # 对数域暗部保护
# relief 路径单独设边界, 不复用模型路径的 [-1.1, +0.4]:
# 那个上界是为了压制模型的镜面过曝而收紧的, 而真实缺陷的亮侧幅度是真实存在的
# (实测真实 p99 均值 1.105, 最大 1.95), 用 0.4 会把真实明暗截掉大半。
# 死白由 ratio_highlight_knee=252 的软限幅兜住(实测可压到 0%)。
RELIEF_CLAMP_LO = -1.2
RELIEF_CLAMP_HI = 1.2
# 位移默认关闭: 它会真的搬动像素, 属于"改画面几何"的操作, 不该在调用方没有
# 显式要求时悄悄生效。config.yaml 里为本批实验设成 4.0, 二者不一致是有意的,
# 因此这两个键刻意不纳入 regression_check 的"config 与常量一致性"检查。
DISPLACE_STRENGTH = 0.0
DISPLACE_SIGMA = 2.0

# 动机: 编辑模型是整块重渲染的, 只要底图上有印刷文字, 它就会把文字重画一遍。
# 实测重画后笔画位置平均偏移约 1.15px, 粗细/对比度也变, 于是羽化过渡带里
# 原文字与重画文字叠加 -> 重影。事后低通能压掉重影, 但同时会把褶皱棱线一起
# 磨平(实测压到重影 10% 时棱线只剩 55%), 两者在频率上重叠, 无法兼得。
#
# 解法: 把低通移到"输入"上而不是"输出差值"上 —— 先把裁块低通成只有光照渐变
# 的平坦底图再交给模型。模型没有文字可画, 只能输出"平坦底色+褶皱明暗";
# 用 编辑结果/平坦底图 求出明暗调制场, 乘到原图上。原图的墨迹像素只被调亮
# 调暗, 位置一动不动 -> 重影在构造上不可能产生, 而褶皱棱线由分子提供,
# 依然是锐利的(只有分母被模糊), 因此不必牺牲锐度。

def flat_sigma(patch_size: tuple[int, int], ratio: float = FLAT_SIGMA_RATIO,
               lo: float = FLAT_SIGMA_MIN, hi: float = FLAT_SIGMA_MAX) -> float:
    """按裁块尺寸决定低通 sigma。

    文字笔画宽度随分辨率等比变化(本项目 .bmp 类约 675 宽, .jpg 类 1200 宽,
    差 1.8 倍), 因此用相对值而非固定像素。
    实测印刷内容边缘残留(重影风险源)随 sigma 变化: 8->15.3%, 12->10.8%,
    20->6.1%, 24->5.0%; 而瓶身光照结构的保留度在 sigma<=40 时始终 ~100%
    (光照尺度是几百像素, 远大于文字)。所以放大 sigma 几乎是免费的,
    没有理由停在 8~12。
    """
    short = min(int(patch_size[0]), int(patch_size[1]))
    return float(np.clip(short * ratio, lo, hi))


def flatten_patch(patch: Image.Image, sigma: float) -> Image.Image:
    """低通抹掉印刷内容, 只留瓶身底色与光照渐变, 作为交给模型的编辑底图。"""
    arr = pil_to_bgr(patch).astype(np.float32)
    out = cv2.GaussianBlur(arr, (0, 0), float(sigma))
    return bgr_to_pil(np.clip(out, 0, 255).astype(np.uint8))


def shading_log_ratio(flat_patch: Image.Image, flat_edited: Image.Image,
                      eps_ratio: float = RATIO_EPS_RATIO,
                      eps_floor: float = RATIO_EPS_FLOOR,
                      clamp_lo: float = RATIO_LOG_CLAMP_LO,
                      clamp_hi: float = RATIO_LOG_CLAMP_HI,
                      strength: float = 1.0) -> np.ndarray:
    """由平坦底图与其编辑结果求明暗调制场, 返回对数域的 float32。

    只取亮度比、不取色度比: 阴影与高光对各通道是等比例调制, 用单一亮度比
    应用到三通道可严格保持原图色度, 也不会把模型的色偏带进结果。

    关于 eps(暗部保护)为什么必须取相对值:
      固定 eps 会对暗区造成远比亮区严重的衰减。实测真实阴影 s=0.6 时,
      eps=12 在底图亮度 10 处只还原 39.3%、亮度 20 处 56.3%, 而亮度 180 处
      有 92.0% —— 这不只是损失, 更是一种随亮度变化的系统性偏差, 会让同一条
      参考在暗类别上稳定地掉 severity。
      取 eps = k*lf 时 (lf*s + k*lf)/(lf + k*lf) = (s+k)/(1+k) 与 lf 无关,
      衰减变成均匀的。k=0.05 时各亮度一律还原约 94%(实测按真实像素分布加权,
      各类别 90.8%~92.6%), 偏差被消除。eps_floor 兜住接近纯黑的极端像素。

    关于 clamp 为什么不对称:
      提亮侧留太大既不物理也危险。项目 prompt 本就要求"受光面亮度不得超过
      周围完好标签", 正向调制理应很小; 而实测乘以 e^1.0=2.72 会让 27.2% 的
      像素超过 255 被裁成死白(1.jpg 类高达 29.8%), 乘 e^0.30=1.35 则降到 5.2%。
      因此上限收到 +0.30, 下限保留 -1.0(阴影压到 37% 是合理的深折痕)。

    关于为什么用"软饱和"而不是硬截断(np.clip):
      硬截断会把所有超限像素钉死在同一个边界值上, 形成一块深度完全一致的平台。
      实测上界取 +0.30 硬截断时, 掩膜内平均有 16.45% 的像素(个别样本达 37.32%)
      被钉在 +0.30 上, 深浅不一的褶皱被统一压成同一档深度 -> 正是质检反复
      反馈的"亮边均匀、浮雕化、深度一致"。
      改成 tanh 软饱和后, 边界只是渐近线, 任何像素都不会取到边界值, 超限部分
      被单调压缩而非抹平, 深浅次序得以保留。上下界都要软化: 只软化一侧会引入
      不对称失真。

    strength 在饱和之前参与, 这样边界始终是真实的安全上限;
    若放到之后再乘, 上限就被绕过了。
    """
    f = pil_to_bgr(flat_patch)
    e = pil_to_bgr(flat_edited)
    if e.shape[:2] != f.shape[:2]:
        e = cv2.resize(e, (f.shape[1], f.shape[0]))
    lf = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32)
    le = cv2.cvtColor(e, cv2.COLOR_BGR2GRAY).astype(np.float32)
    eps = np.maximum(float(eps_floor), float(eps_ratio) * lf)
    L = np.log((le + eps) / (lf + eps)) * float(strength)

    return soft_saturate(L, clamp_lo, clamp_hi)


def soft_saturate(x: np.ndarray, lo: float, hi: float) -> np.ndarray:
    """把 x 用 tanh 软饱和到 (lo, hi) 开区间, 边界只是渐近线。

    与 np.clip 的区别: 硬截断会把所有超限像素钉死在同一个值上, 形成深度完全
    一致的平台(实测上界 +0.30 硬截断时掩膜内平均 16.45% 的像素被钉住, 个别
    样本 37.32%), 观感就是"亮边均匀、浮雕化、深浅一致"。软饱和保留单调次序,
    超限部分被压缩而非抹平。
    """
    hi = abs(float(hi))
    lo = -abs(float(lo))
    pos = hi * np.tanh(np.maximum(x, 0.0) / max(hi, 1e-6))
    neg = lo * np.tanh(np.minimum(x, 0.0) / min(lo, -1e-6))
    return (pos + neg).astype(np.float32)


def mask_from_log_ratio(log_ratio: np.ndarray, thresh: float = RATIO_MASK_THRESH,
                        min_area_ratio: float = 0.0006,
                        return_info: bool = False):
    """由明暗调制强度求缺陷掩膜(uint8 0/255)。

    与 diff_defect_mask 的区别: 输入不含印刷内容, 所以不存在"文字被重画"
    造成的伪差分, 掩膜天然只框住褶皱本身 —— 顺带解决了标注 bbox 被文字
    错位噪声虚增的问题。也因此不再需要面积上限那个粗糙判据。
    """
    mag = np.abs(log_ratio)
    m = (mag > float(thresh)).astype(np.uint8) * 255
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, kernel, iterations=2)
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, kernel, iterations=1)

    total = m.shape[0] * m.shape[1]
    num, labels, stats, _ = cv2.connectedComponentsWithStats(m, connectivity=8)
    out = np.zeros_like(m)
    for i in range(1, num):
        if int(stats[i, cv2.CC_STAT_AREA]) >= min_area_ratio * total:
            out[labels == i] = 255

    info = {
        # 字段名与 diff_defect_mask 保持一致, 使 annotations 的结构不变
        "diff_mean": round(float(mag.mean()), 4),
        "over_thresh_ratio": round(float((mag > thresh).mean()), 4),
        "prefilter_ratio": round(float((m > 0).mean()), 4),
        "oversized_count": 0,
        "fallback_used": False,
        "mask_ratio": round(float((out > 0).mean()), 4),
    }
    return (out, info) if return_info else out


def _place_centered(field: np.ndarray, out_h: int, out_w: int) -> np.ndarray:
    """把 field 居中放进 (out_h, out_w); 更大就中心裁, 更小就居中补零。

    刻意不做 stretch-to-fit: 非等比拉伸会改变褶皱的走向角度, 而"走向与参考
    一致"正是这条路线唯一要保住的东西, 拉伸等于亲手破坏它。
    """
    h, w = field.shape
    out = np.zeros((out_h, out_w), np.float32)
    sy, sx = max(0, (h - out_h) // 2), max(0, (w - out_w) // 2)
    dy, dx = max(0, (out_h - h) // 2), max(0, (out_w - w) // 2)
    ch, cw = min(h, out_h), min(w, out_w)
    out[dy:dy + ch, dx:dx + cw] = field[sy:sy + ch, sx:sx + cw]
    return out


def reference_shading_field(ref_crop: Image.Image, box: Box,
                            target_full_size: tuple[int, int],
                            ref_source_size: tuple[int, int] | None = None,
                            sigma_small: float = RELIEF_SIGMA_SMALL,
                            sigma_large: float = RELIEF_SIGMA_LARGE,
                            amplitude: float = RELIEF_AMPLITUDE,
                            clamp_lo: float = RELIEF_CLAMP_LO,
                            clamp_hi: float = RELIEF_CLAMP_HI,
                            eps: float = RELIEF_EPS) -> np.ndarray:
    """从参考缺陷裁剪里直接提取真实的明暗调制场(对数域), 对齐到目标裁块。

    为什么在对数域提取, 而不是复用 structure_ref 渲染好的灰度浮雕图:
      1) 浮雕图内部做了"按 p99 归一化到 ±1 再乘 ~126"的处理, 会把真实深浅
         差异抹平。实测 5 条参考的真实幅度从 0.76 到 1.69(2.2 倍差距), 渲染成
         浮雕图后全部落在 0.987~0.992(差异仅 0.5%), 变异系数从 0.343 掉到
         0.002 —— severity 信息在那一步就永久丢失, 下游再乘任何系数都补不回来。
      2) 浮雕图的带通在线性域做。线性域里 g = 反射率 x 明暗, 二者不可分离:
         实测把裁剪整体亮度乘 0.5(模拟深色标签), 线性域带通幅度直接掉 50%,
         而对数域只掉 15~19%。也就是说线性域提取的幅度被源瓶的反射率图案污染,
         同一个物理褶皱长在深色标签上会被提取得更浅。
         对数域 log(反射率 x 明暗) = log(反射率) + log(明暗) 才真正分离,
         这与乘性回贴用对数域是同一个理由, 两端应当统一。

    尺度对齐: 按"目标图与参考源图的分辨率之比"等比缩放。参考裁剪的像素就是
    源图像素, 所以这个比例能让缺陷在目标瓶上占据与在源瓶上相同的物理比例,
    与 adaptive_patch_size 选裁块尺寸用的是同一套换算。

    amplitude 是倍率而非绝对目标值: 1.0 表示忠实还原真实幅度, 从而保留不同
    参考之间真实存在的深浅差异。
    """
    g = np.array(ref_crop.convert("L")).astype(np.float32)
    lg = np.log(g + float(eps))
    band = (cv2.GaussianBlur(lg, (0, 0), float(sigma_small))
            - cv2.GaussianBlur(lg, (0, 0), float(sigma_large)))

    f = 1.0
    if ref_source_size:
        sw, sh = float(ref_source_size[0]), float(ref_source_size[1])
        tw, th = float(target_full_size[0]), float(target_full_size[1])
        if sw > 0 and sh > 0:
            f = 0.5 * (tw / sw + th / sh)     # 单一比例, 保证等比
    if abs(f - 1.0) > 1e-3:
        nh = max(1, int(round(band.shape[0] * f)))
        nw = max(1, int(round(band.shape[1] * f)))
        band = cv2.resize(band, (nw, nh), interpolation=cv2.INTER_LINEAR)

    field = _place_centered(band, box.h, box.w) * float(amplitude)
    return soft_saturate(field, clamp_lo, clamp_hi)


def jitter_shading_field(field: np.ndarray, rng: np.random.Generator, *,
                         flip_h: bool = False, flip_v: bool = False,
                         rot_deg: float = 0.0, scale: float = 1.0,
                         elastic_amp: float = 0.0,
                         elastic_sigma: float = 0.0) -> np.ndarray:
    """对搬运来的明暗场做受控扰动, 破掉"同一条参考产出逐像素相同几何"。

    为什么必须做这件事:
      relief transfer 是确定性的 —— 同一条参考在 N 张干净图上会产出几何完全
      相同的褶皱, 只有底色/光照/位置不同。若一批产出里大量样本共享同一条参考,
      训练集在"褶皱几何"这一维的多样性会明显收窄, 模型可能学到"这几种特定
      形状"而不是"褶皱这一类缺陷的一般特征"。
      这个代价是 relief transfer 独有的: 模型路径虽然几何不对, 但每次画的
      几何都不同, 客观上提供了(虚假的)几何多样性。

    各扰动手段的性质(决定了该扫描哪一个):
      镜像 / 旋转: 对走向直方图只是置换或环移, 方向熵与集中度在数学上完全
        不变, 却改变了实际几何路径 —— 相当于免费的多样性。
      缩放: 保持角度不变, 改变折痕的宽度与间距尺度。
      弹性形变: 唯一能真正改变每条折痕走行路径的手段, 也是唯一可能破坏真实性
        的手段。其空间尺度(elastic_sigma)比幅度更关键: 大尺度+中等幅度是整体
        弯折, 局部折痕形状仍完好; 小尺度+大幅度会把折痕揉碎成不自然的形状。

    边界填 0 是安全的: 明暗场里 0 表示"不调制", 所以旋转/形变带进来的空白
    区域不会产生任何伪影。
    """
    out = field.astype(np.float32)
    if flip_h:
        out = out[:, ::-1].copy()
    if flip_v:
        out = out[::-1, :].copy()

    h, w = out.shape
    if abs(rot_deg) > 1e-6 or abs(scale - 1.0) > 1e-6:
        m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), float(rot_deg), float(scale))
        out = cv2.warpAffine(out, m, (w, h), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)

    if float(elastic_amp) > 1e-6:
        sig = float(elastic_sigma) if float(elastic_sigma) > 0 else max(8.0, min(h, w) * 0.08)
        nx = rng.standard_normal((h, w)).astype(np.float32)
        ny = rng.standard_normal((h, w)).astype(np.float32)
        nx = cv2.GaussianBlur(nx, (0, 0), sig)
        ny = cv2.GaussianBlur(ny, (0, 0), sig)
        for n in (nx, ny):
            s = float(np.abs(n).max())
            if s > 1e-9:
                n /= s
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        out = cv2.remap(out, x + nx * float(elastic_amp), y + ny * float(elastic_amp),
                        cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                        borderValue=0.0)
    return out


def displacement_field(log_ratio: np.ndarray, alpha: np.ndarray,
                       strength_px: float = DISPLACE_STRENGTH,
                       sigma: float = DISPLACE_SIGMA):
    """按明暗场构造几何位移场, 返回 (dx, dy) 或 None(强度为0/退化时)。

    为什么需要位移: flat-edit 的乘性回贴只改亮度、像素位置一动不动, 而真实
    褶皱一定会把印刷内容推弯。缺了这一步, 即便明暗几何与参考完全一致, 人眼
    看上去也只是"贴了一层阴影"(质检反复反馈"印刷线条不随形变扭曲")。

    物理模型: 把去噪后的明暗场当作高度场的代理(与 structure_ref 浮雕图同一
    约定: 亮=凸起, 暗=凹陷)。小坡度下, 表面起伏引起的贴图面内位移 ≈ k·∇h,
    所以方向与幅度都取自 ∇s:
      - 棱线顶 / 谷底: |∇s|≈0 -> 位移≈0。正确, 那里表面朝向没有改变。
      - 折痕陡侧:     |∇s| 最大 -> 位移最大。正确, 那里贴图被推挤得最厉害。

    这里刻意不沿用之前那版实现(方向取 ∇s 的单位向量、幅度取 s 本身):
    二者不自洽 —— 在极值点幅度最大, 而方向恰好最不确定(|∇s|→0 时归一化的
    数值不稳定, 只靠 +1e-6 兜底), 等于在最不该位移、方向又最随机的位置
    施加最大位移。

    幅度按掩膜内 |∇s| 的 95 分位归一化, 使 strength_px 的含义就是"最大位移
    像素数", 不随裁块尺寸、褶皱空间尺度或 clamp 边界变化而漂移(旧实现的幅度
    直接正比于 s, 因此改 clamp 边界会连带改掉位移强度, 两个旋钮耦合在一起)。
    strength_px 取负值可翻转弯曲方向(朝亮侧还是暗侧), 需看图确定。
    """
    if abs(float(strength_px)) < 1e-6:
        return None
    s = log_ratio.astype(np.float32)
    if float(sigma) > 0:
        s = cv2.GaussianBlur(s, (0, 0), float(sigma))
    gy, gx = np.gradient(s)
    mag = np.hypot(gx, gy)

    inside = alpha > 0.02
    ref = mag[inside] if inside.sum() > 50 else mag
    scale = float(np.percentile(ref, 95)) if ref.size else 0.0
    if scale < 1e-9:
        return None

    # 按 95 分位归一化后, 再把单轴分量裁到 ±strength_px。裁剪上限刻意取
    # 1 倍而不是 2 倍: 超过 95 分位的那 5% 像素会被裁到恰好 strength_px,
    # 于是"最大位移 = strength_px"这个标称含义是准确的。上限取 2 倍时实测
    # 99 分位位移会达到标称值的约 1.84 倍(标称 4.0 实际 7.37px), 参数名义与
    # 实际不符, 靠看图调参时会误判。
    k = float(strength_px) / scale
    lim = abs(float(strength_px))
    dx = np.clip(gx * k, -lim, lim) * alpha
    dy = np.clip(gy * k, -lim, lim) * alpha
    return dx.astype(np.float32), dy.astype(np.float32)


def ratio_composite(orig_full: Image.Image, log_ratio: np.ndarray,
                    alpha: np.ndarray, box: Box,
                    highlight_knee: float = RATIO_HIGHLIGHT_KNEE,
                    displace_strength: float = DISPLACE_STRENGTH,
                    displace_sigma: float = DISPLACE_SIGMA) -> Image.Image:
    """乘性回贴: result = 原图(可先几何位移) × exp(alpha × log_ratio), 提亮侧带软限幅。

    只调制明暗, 不替换任何像素内容:
      - 印刷文字/图案逐像素留在原位 -> 不可能出现重影或内容跳变
      - 三通道同乘一个系数 -> 色度严格不变, 也不会有加性带来的色偏/晕圈
      - 乘性更符合物理: 阴影是反射光 ×(<1), 高光是 ×(>1)

    提亮侧软限幅(knee 以上用 tanh 压缩)的必要性:
      旧的加性回贴是凸组合(原图与编辑图的加权平均), 两端都在 [0,255] 内,
      结构上不可能溢出, 所以那里不需要限幅。乘性会真的越界: 原图本就接近
      白的像素(如 240)哪怕只乘 1.2 也会超 255。直接硬裁会让一片区域全部变成
      同一个 255, 细节层次彻底消失 -> 观感就是一块死白。
      这里用 tanh 软膝压缩: 单调、渐近趋近 255 但不触及。
      暗侧不需要处理: region>=0 且 gain>0, 乘性结果不可能为负。

      膝点必须定得高(默认 252), 只当纯安全网。原因: clamp 收到 +0.30 之后
      真正溢出的像素只剩约 0.02%, 膝点若定低(如 235)会把大量本来完好的亮部
      像素(235~255, 根本不需要压)一并压进很窄的输出范围, 经 uint8 量化后互相
      碰撞, 亮部明暗次序反而比硬裁更差。实测(统一在 target>245 区间评估):
        膝点255(不限幅): 死白 0.021%, 死白最大连通 102px, 次序保留 0.9564
        膝点252        : 死白 0%,     连通 0,             次序保留 0.9320
        膝点235        : 死白 0%,     连通 0,             次序保留 0.8950
      252 已能完全消灭死白连片, 再往下压没有额外收益、纯损失亮部层次。
    """
    base = np.array(orig_full.convert("RGB")).astype(np.float32)
    region = base[box.y:box.y2, box.x:box.x2].copy()

    L = log_ratio
    if L.shape != (box.h, box.w):
        L = cv2.resize(L, (box.w, box.h))
    a = alpha.astype(np.float32) / 255.0
    if a.shape != (box.h, box.w):
        a = cv2.resize(a, (box.w, box.h))

    # 几何位移(见 displacement_field 的说明)。位移场已按 alpha 衰减, 掩膜外
    # alpha=0 -> 位移为 0 -> remap 退化为恒等映射, 掩膜外像素不动, 硬约束不破坏。
    # displace_strength=0 时整段跳过, 行为与"纯明暗"完全一致。
    d = displacement_field(L, a, displace_strength, displace_sigma)
    if d is not None:
        dx, dy = d
        h, w = L.shape
        y, x = np.mgrid[0:h, 0:w].astype(np.float32)
        region = cv2.remap(region, x + dx, y + dy, cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REFLECT)

    target = region * np.exp(a * L)[..., None]

    knee = float(highlight_knee)
    if knee < 255.0:
        head = 255.0 - knee
        over = target > knee
        if over.any():
            target = np.where(
                over, knee + head * np.tanh((target - knee) / head), target)

    base[box.y:box.y2, box.x:box.x2] = target
    return Image.fromarray(np.clip(base, 0, 255).astype(np.uint8))


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
