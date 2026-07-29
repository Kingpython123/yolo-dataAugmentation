"""缺陷结构图(浮雕图): 从参考缺陷裁剪中只提取"形变几何", 剥离外观。

动机
----
直接把另一个瓶子的彩色实拍裁剪当参考, 模型会连同源瓶的标签图案、颜色, 以及
源瓶自身的镜面高光带一起照抄, 表现为"缺陷有过曝亮边"。

原理
----
按空间频率分离:
  低频(sigma_large) = 整体光照渐变、瓶身明暗   -> 要去掉
  中频             = 褶皱/折痕的明暗起伏      -> 要保留
  高频(sigma_small) = 印刷文字笔画、噪点       -> 要去掉
带通(Difference of Gaussian) 即可只留中频, 再以中性灰为基准渲染成灰度浮雕图:
  128 = 未形变的平面, 亮 = 凸起, 暗 = 凹陷
输出不含任何颜色、印刷内容与绝对亮度, 因此不会把源瓶的外观/高光带入目标。
"""
from __future__ import annotations

import cv2
import numpy as np
from PIL import Image


def defect_structure_map(crop: Image.Image,
                         sigma_small: float = 6.0,
                         sigma_large: float = 40.0,
                         gain: float = 3.0,
                         clip_pct: float = 99.0,
                         max_side: int = 1024) -> Image.Image:
    """把缺陷裁剪转成灰度"形变浮雕图"(只含褶皱几何, 无颜色/图案/高光)。

    参数
    ----
    sigma_small: 高频截断; 越大越能抹掉印刷文字笔画
    sigma_large: 低频截断; 越小越能压掉整体光照渐变
    gain:        对比增益, 让褶皱起伏看得清
    clip_pct:    鲁棒归一化的百分位, 防止个别极值把整图压平
    """
    img = crop.convert("L")
    if max(img.size) > max_side:  # 控制尺寸, 顺带再抑制一点高频噪声
        r = max_side / max(img.size)
        img = img.resize((max(1, int(img.width * r)),
                          max(1, int(img.height * r))))
    g = np.array(img).astype(np.float32)

    # 带通: 去高频(文字) + 去低频(光照渐变), 只留中频(褶皱起伏)
    lo = cv2.GaussianBlur(g, (0, 0), sigma_small)   # 去掉笔画级高频
    hi = cv2.GaussianBlur(g, (0, 0), sigma_large)   # 整体光照基准
    band = lo - hi

    # 鲁棒归一化到 ±1, 再以中性灰 128 为基准渲染
    scale = float(np.percentile(np.abs(band), clip_pct))
    if scale < 1e-6:
        scale = 1.0
    band = np.clip(band / scale, -1.0, 1.0)
    # 以中性灰 128 为基准: 亮=凸起, 暗=凹陷; 幅度受 gain 控制且不越界
    relief = 128.0 + band * min(127.0, 42.0 * gain)
    out = np.clip(relief, 0, 255).astype(np.uint8)
    return Image.fromarray(out, mode="L").convert("RGB")


def structure_map_stats(crop: Image.Image, **kw) -> dict:
    """用于离线验证: 衡量结构图保留了多少中频、抑制了多少高频/低频。"""
    m = defect_structure_map(crop, **kw)
    a = np.array(m.convert("L")).astype(np.float32)
    g = np.array(crop.convert("L").resize(m.size)).astype(np.float32)

    def energy(x, s1, s2):
        lo = cv2.GaussianBlur(x, (0, 0), s1)
        hi = cv2.GaussianBlur(x, (0, 0), s2)
        return float(np.abs(lo - hi).mean())

    return {
        "src_high": energy(g, 0.8, 3.0),     # 文字笔画级
        "map_high": energy(a, 0.8, 3.0),
        "src_mid": energy(g, 3.0, 40.0),     # 褶皱级
        "map_mid": energy(a, 3.0, 40.0),
        "src_lowspread": float(cv2.GaussianBlur(g, (0, 0), 40).std()),
        "map_lowspread": float(cv2.GaussianBlur(a, (0, 0), 40).std()),
        "map_mean": float(a.mean()),
    }
