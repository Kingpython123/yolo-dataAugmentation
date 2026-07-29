"""质检: 参考对比式多维评分 + 掩膜外像素变化硬校验。

与旧版的关键区别:
  1. 把【参考缺陷图】一起送进去, 从"有没有缺陷"升级为"像不像参考"。
  2. 送【裁块级全分辨率】对比图, 而非整图缩略图, 让模型真的看清缺陷。
  3. 多维独立打分(类型/形状/强度/融合), 并给出评分锚点强制拉开分差。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from PIL import Image

from .api_client import RelayClient
from .config import Config
from . import mask_utils

QC_PROMPT = """你是极其严格的工业缺陷合成质检专家。我在把一个真实缺陷"复刻"到一张干净的瓶子图上。

给你三张图:
  图1 = 参考缺陷(真实拍到的缺陷, 这是要复刻的标准答案)
  图2 = 目标区域合成前(干净)
  图3 = 目标区域合成后(待评估的结果)

参考缺陷的规格: 类型={dtype}, 严重度={severity}/5, 条数≈{count}, 走向={orientation}
形态: {geometry}

请对【图3 相比图2 新增的缺陷】评估, 输出严格 JSON(不要多余文字):
{{
  "defect_present": true/false,     // 图3 是否确实出现了新缺陷
  "type_match": 1-10,              // 是否是"表面形变(褶皱/折痕/凹陷)"; 做成了污渍/脏斑点/异物/裂纹则≤3
  "shape_fidelity": 1-10,          // 形状/走向/条数 与参考的贴合度(仅记录, 非主要门槛)
  "severity_match": 1-10,          // 强度是否达到参考的严重度(明显更淡更弱则≤4)
  "highlight_natural": 1-10,       // 缺陷的高光是否自然
  "seam_continuity": 1-10,         // 缺陷区与周围瓶身是否连续一致
  "blend_quality": 1-10,           // 整体融合质量
  "only_local_change": true/false, // 是否只有局部变化, 整体光照/形状/背景未被改动
  "on_bottle": true/false,         // 缺陷是否长在瓶身上(若画在桌布/背景上则为 false)
  "artifacts": "列出所有不自然的痕迹, 没有则空字符串",
  "verdict": "一句总体判断"
}}

【评分锚点, 必须严格执行, 禁止一律给7分, 各维度分数不要雷同】
- highlight_natural: 缺陷上出现过亮的白色/镜面高光、亮边、局部曝光过度、
  比周围瓶身明显更亮更刺眼 -> 最高给 3。柔和低对比的漫反射明暗过渡 -> 8以上。
- seam_continuity: 缺陷区域的印刷文字/图案/颜色与上下左右的瓶身接不上、
  有错位/重绘/断层/像贴上去的一块 -> 最高给 3。完全连续一致 -> 8以上。
- type_match: 若生成的是脏污斑点、点状污迹、异物、划伤而不是薄膜形变 -> 最高给 3。
- severity_match: 缺陷明显比参考更淡/更弱 -> 最高给 4。
- 注意: 缺陷"覆盖面积大、变形夸张"本身不是缺点, 真实样本中就存在大面积严重变形,
  只要看起来真实即可, 不要因为面积大或变形夸张而扣分。
- blend_quality: 综合考虑, 只有确实难分真假才给 8 以上。"""

# 参与评分的维度(顺序固定, 权重与判定都按这个集合展开)
DIMS = ("type_match", "shape_fidelity", "severity_match",
        "highlight_natural", "seam_continuity", "blend_quality")

# 默认权重: 偏向人工审核实际暴露的两个问题(高光突兀 / 接缝断层)
DEFAULT_WEIGHTS = {"type_match": 0.20, "shape_fidelity": 0.05,
                   "severity_match": 0.15, "highlight_natural": 0.25,
                   "seam_continuity": 0.25, "blend_quality": 0.10}


def dim_weights(cfg: Config) -> dict[str, float]:
    w = cfg.qc.get("weights") or DEFAULT_WEIGHTS
    return {k: float(w.get(k, DEFAULT_WEIGHTS[k])) for k in DIMS}


def gating_dims(cfg: Config) -> list[str]:
    """真正参与判定的维度(即 min_scores 里设了下限的)。"""
    mins = cfg.qc.get("min_scores", {}) or {}
    return [k for k in DIMS if mins.get(k) is not None]


def judge(cfg: Config, scores: dict, *, defect_present: bool = True,
          only_local: bool = True, on_bottle: bool = True,
          bg_change: float = 0.0) -> tuple[bool, float, list[str]]:
    """纯判定: 由已有分数按当前配置算出 (是否合格, 综合分, 失败原因)。

    不调用任何 API。check() 与 requalify 共用此函数, 保证在线判定与离线
    复判的规则完全一致 —— 改阈值只需改 config, 两条路径自动同步。
    """
    qc = cfg.qc
    tw = dim_weights(cfg)
    tot = sum(tw.values()) or 1.0
    realism = round(sum(_num(scores, k) * tw[k] for k in DIMS) / tot, 2)

    fails: list[str] = []
    if bg_change > qc.get("max_bg_change_ratio", 0.02):
        fails.append(f"掩膜外变化过大({bg_change:.4f})")
    if not defect_present:
        fails.append("未检出新增缺陷")
    # only_local: 默认不再用 VLM 的主观判断当闸门。"是否只改了局部"已经由
    # bg_change 这个逐像素指标严格验证过(掩膜外变化比例), 而 VLM 看到大面积
    # 变形会主观答"非局部" —— 实测 realism 7.5/6.85 且各维度全部过线的样本
    # 仅因这一条被驳回, 而它们的 bg_change 是 0.0。像素指标比主观判断可靠。
    if qc.get("gate_only_local", False) and not only_local:
        fails.append("非局部变化")
    # on_bottle: 默认不当闸门。缺陷落到桌布上不影响使用(人工标注时不打框),
    # 且该判断依赖 VLM 分辨桌布与瓶身背光面, 二者灰度高度重叠, 并不可靠。
    if qc.get("gate_on_bottle", False) and not on_bottle:
        fails.append("缺陷未长在瓶身上")
    mins = qc.get("min_scores", {}) or {}
    for k in DIMS:
        lo = mins.get(k)
        if lo is not None and _num(scores, k) < lo:
            fails.append(f"{k}={_num(scores, k):g}<{lo}")
    floor = qc.get("min_realism_score", 7)
    if realism < floor:
        fails.append(f"综合分={realism}<{floor}")
    return len(fails) == 0, realism, fails


@dataclass
class QCResult:
    passed: bool
    realism: float
    defect_present: bool
    only_local: bool
    bg_change: float
    reason: str
    scores: dict = field(default_factory=dict)
    fail_reasons: list = field(default_factory=list)
    on_bottle: bool = True

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "realism": self.realism,
            "scores": self.scores,
            "defect_present": self.defect_present,
            "only_local": self.only_local,
            "on_bottle": self.on_bottle,
            "bg_change": round(self.bg_change, 5),
            "fail_reasons": self.fail_reasons,
            "reason": self.reason,
        }

    # 实际导致驳回的维度(由 check() 填入), 重试只针对这些做强化
    gating_dims: list = field(default_factory=list)

    @property
    def weakest(self) -> str:
        """最弱的"有门槛"维度, 供重试时针对性强化。

        只考虑真正参与判定的维度: 追一个不设门槛的维度(如 shape_fidelity)
        既改善不了判定结果, 还会白烧一次 API 调用。
        """
        if not self.scores:
            return ""
        pool = [k for k in self.gating_dims if k in self.scores] or list(self.scores)
        return min(pool, key=lambda k: self.scores[k])


def _safe_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        text = text[s:e + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _num(d: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(d.get(key, default) or default)
    except (TypeError, ValueError):
        return default


def _thumb(im: Image.Image, long_side: int) -> Image.Image:
    r = long_side / max(im.size)
    if r < 1:
        im = im.resize((int(im.width * r), int(im.height * r)))
    return im


def check(cfg: Config, relay: RelayClient, orig_full: Image.Image,
          result_full: Image.Image, defect_mask,
          ref: dict | None = None,
          orig_patch: Image.Image | None = None,
          result_patch: Image.Image | None = None,
          ref_crop: Image.Image | None = None) -> QCResult:
    qc = cfg.qc

    # ---- 1) 硬约束: 掩膜外像素变化比例(纯像素, 不依赖模型) ----
    bg_change = mask_utils.background_change_ratio(
        orig_full, result_full, defect_mask,
        pixel_thresh=qc.get("bg_change_pixel_thresh", 12))
    bg_ok = bg_change <= qc.get("max_bg_change_ratio", 0.02)

    if not qc.get("enable", True):
        return QCResult(bg_ok, 10.0, True, True, bg_change, "QC 关闭, 仅像素校验")

    # ---- 2) 多维参考对比评分 ----
    ref = ref or {}
    prompt = QC_PROMPT.format(
        dtype=ref.get("defect_type", "未知"),
        severity=ref.get("severity", "?"),
        count=ref.get("count", "?"),
        orientation=ref.get("orientation") or "未指定",
        geometry=ref.get("geometry") or ref.get("appearance") or "未指定")

    max_side = qc.get("qc_image_long_side", 1024)
    images: list[Image.Image] = []
    if ref_crop is not None:
        images.append(_thumb(ref_crop, max_side))
    if orig_patch is not None and result_patch is not None:
        images.append(_thumb(orig_patch, max_side))
        images.append(_thumb(result_patch, max_side))
    else:  # 兜底: 没给裁块时退回整图
        images.append(_thumb(orig_full, max_side))
        images.append(_thumb(result_full, max_side))

    # 质检没跑成 != 质检通过。中转站抖一下就放行, 会让未经检验的样本混进训练集,
    # 因此一律判不合格并标记原因, 留给 requalify --rescore 事后补判。
    try:
        data = _safe_json(relay.chat_vision(prompt, images=images,
                                            expect_json=True))
    except Exception as e:
        return QCResult(False, 0.0, False, bg_ok, bg_change,
                        f"VLM质检异常: {e}", {}, ["质检未完成(VLM异常)"])
    if not data:
        return QCResult(False, 0.0, False, bg_ok, bg_change,
                        "VLM质检返回无法解析", {}, ["质检未完成(返回无法解析)"])

    dims = DIMS
    scores = {k: _num(data, k) for k in dims}
    defect_present = bool(data.get("defect_present", False))
    only_local = bool(data.get("only_local_change", False))
    on_bottle = bool(data.get("on_bottle", True))

    # ---- 3) 判定: 综合分 + 各维度下限 + 硬约束(与离线复判同一套规则) ----
    passed, realism, fails = judge(
        cfg, scores, defect_present=defect_present, only_local=only_local,
        on_bottle=on_bottle, bg_change=bg_change)

    reason = str(data.get("verdict", "")).strip()
    if data.get("artifacts"):
        reason += f" | 痕迹:{data['artifacts']}"

    res = QCResult(passed, realism, defect_present, only_local,
                   bg_change, reason.strip(), scores, fails)
    res.on_bottle = on_bottle
    # 只有设了下限的维度才参与"最弱维度"选择(重试定向强化的依据)
    res.gating_dims = gating_dims(cfg)
    return res
