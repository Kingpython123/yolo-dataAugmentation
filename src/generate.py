"""主生成流程: 对无缺陷图逐张合成逼真缺陷。

单张循环:
  分割瓶身 -> 按参考缺陷自适应裁块 -> 参考引导编辑 -> 光度对齐
  -> 差分求掩膜 -> 增益融合 -> 参考对比式多维质检 -> 合格落盘 / 分级重试
"""
from __future__ import annotations

import datetime
import json
import random
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from . import mask_utils, quality_check, reporting, structure_ref
from .api_client import RelayClient
from .config import Config
from .dataset import safe_name, scan_class_images
from .defect_catalog import load_catalog

EDIT_PROMPT_TMPL = """You are an industrial defect synthesis expert.
Image 1 = CLEAN target patch of a bottle surface (to be edited).
Image 2 = REFERENCE photo of a REAL "{dtype}" defect (on a DIFFERENT bottle).
{structure_note}
IMPORTANT — how to use the references:
- Take the defect's GEOMETRY ONLY from the reference(s): where the creases run, how
  many there are, how they branch and how deep the undulation is.
- Take EVERYTHING ELSE from image 1: material, base colour, printed artwork,
  light direction, overall brightness and glossiness.
- Do NOT copy the reference's colours, its printed graphics, or its highlights.
  The reference bottle was shot under its own lighting; that lighting is NOT valid here.

=== REFERENCE DEFECT SPECIFICATION (reproduce this precisely) ===
Type: {dtype}
Overall: {appearance}
Severity: {severity}/5  (5 = extremely pronounced; you MUST match this level)
Count: about {count} creases/lines
Orientation: {orientation}
Geometry: {geometry}
Extent: {extent}
Light & shade (describes the SOURCE photo — reproduce the light/dark STRUCTURE it
implies, but re-render it with the target's own diffuse lighting, and treat any
mention of bright highlights as diffuse shading, NOT as specular glare): {photometry}
Edge profile: {edge_profile}
Effect on printed artwork: {texture_effect}
Hint: {prompt_hint}
================================================================

GOAL: Reproduce that defect on the TARGET AS FAITHFULLY AS POSSIBLE (1:1).
Match the exact shape, orientation, count, layout, extent and especially the SEVERITY
described above. Keep it as strong, deep and clearly visible as the reference —
do NOT soften, shrink, simplify, or make it subtler. The defect must be obvious.

Adapt ONLY the illumination: the defect's own highlights and shadows must follow the
TARGET patch's light direction and glossiness so it sits physically on this surface;
match the target's material, base color and reflections.

HARD constraints:
- Keep the target's global lighting, geometry, size, material, color and background
  EXACTLY the same. Only add the localized defect.
- Blend seamlessly: no visible rectangle/patch edge, no halo, no copy-paste look.
- Output the edited patch at the SAME resolution and framing as image 1.

CRITICAL — the following are the most common failures, avoid them:
1. The creases must be STRUCTURALLY STRONG but NOT SPECULAR. These are two separate
   things, satisfy BOTH:
   (a) DO make the deformation clearly readable: deep, well-defined folds with distinct
       light-and-shade relief, obvious at a glance. Do not flatten or wash it out.
   (b) Do NOT render it with mirror-like specular reflections: no pure-white or
       blown-out hot spots, no glowing rims, no chrome/metal-foil sheen. This is a
       MATTE / satin label film lit by soft diffuse light, so the relief must be built
       from DIFFUSE shading — the shadow side may go clearly darker, but the lit side
       must stay within the brightness range of the surrounding label, never brighter.
   In short: strong shape and strong shadows, but no glare.
2. PRESERVE the printed artwork. Do NOT redraw, restyle, re-letter or shift the printed
   text, logo, colours or graphics. Every letter and edge must remain exactly where it is,
   only locally bent/compressed where a crease physically passes over it. Any change to
   the artwork itself makes the patch not line up with the rest of the bottle.
3. Keep the bottle's silhouette, edges and vertical alignment identical so this patch
   still matches the bottle above and below it.
4. This must be a SURFACE DEFORMATION (wrinkle/crease/dent) of the film itself.
   Do NOT produce dirt, stains, dark spots, specks, scratches or foreign particles.
Return only the edited image."""

STRUCTURE_NOTE = """Image 3 = GREY RELIEF MAP of the same defect: mid-grey is the
undeformed surface, lighter = raised, darker = sunken. It contains ONLY the crease
geometry — no colour, no printed content, no lighting from the source bottle.
Use image 3 as the authoritative guide for the SHAPE and LAYOUT of the creases."""

# 按上一轮质检最弱维度追加的强化指令(分级重试)
ESCALATE_HINTS = {
    "type_match": "\n\nPREVIOUS ATTEMPT FAILED: the defect type was wrong. It MUST be a "
                  "surface deformation (wrinkle/crease/dent) of the label film — NOT dirt, "
                  "stains, spots, specks, foreign particles or scratches.",
    "severity_match": "\n\nPREVIOUS ATTEMPT FAILED: the deformation was TOO WEAK and too "
                      "subtle. Make the folds clearly DEEPER and more pronounced (match "
                      "severity {severity}/5) — increase the relief and the DARK shadow side "
                      "of each crease, but still WITHOUT any bright specular glare.",
    "highlight_natural": "\n\nPREVIOUS ATTEMPT FAILED: the defect had unnatural blown-out "
                         "highlights / glaring bright edges. Rebuild the SAME deformation using "
                         "only diffuse shading: keep the shadow side, but the lit side must NOT "
                         "exceed the brightness of the surrounding undamaged label. No white "
                         "hot spots, no glowing rims, no metallic sheen.",
    "seam_continuity": "\n\nPREVIOUS ATTEMPT FAILED: the edited area did not line up with the "
                       "rest of the bottle. Keep the printed text, graphics, colours and the "
                       "bottle edges exactly as in image 1 — they may only bend locally where a "
                       "crease crosses them. Nothing may be re-drawn, re-coloured or shifted.",
    "blend_quality": "\n\nPREVIOUS ATTEMPT FAILED: it looked pasted on (sticker/overlay look). "
                     "Integrate it into the surface with physically correct diffuse shading "
                     "consistent with image 1's own lighting, and no halo.",
    "shape_fidelity": "\n\nPREVIOUS ATTEMPT FAILED: the shape did not match. Follow the "
                      "reference's outline, orientation and number of creases much more closely.",
    # 掩膜为空但模型改动巨大时用: 问题是"重绘了整块", 不是"画得太弱"
    "over_repaint": "\n\nPREVIOUS ATTEMPT FAILED: you re-rendered the WHOLE patch instead "
                    "of adding a localized defect. Almost every pixel changed, which makes "
                    "the edit unusable. Keep the deformation just as strong, but CONFINE the "
                    "changes to the creased area only: every pixel outside the creases — the "
                    "printed text, logo, colours, brightness and texture — must stay "
                    "byte-for-byte as in image 1. Do not repaint, restyle or re-expose the "
                    "undamaged surface.",
}


# --------------------------- 小工具 ---------------------------

def _f(rec: dict, key: str, default: str = "not specified") -> str:
    """取字段, 兼容旧版缺陷库(缺少细化字段时给占位值)。"""
    v = rec.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        return default
    return str(v).strip()


_safe = safe_name  # 目录/文件名净化(与 requalify 共用同一实现)

# 取消标记。用一个不可能与真实错误信息重合的哨兵值, 让主循环把"用户主动
# 停止"与"任务失败"区分开: 前者不写 fail_log, 也不计入失败率统计, 否则
# 一次手动停止会在失败日志里留下几百条噪声, 把真正的失败原因埋掉。
CANCELLED = "__cancelled__"


def _to_arr(mask_img: Image.Image) -> np.ndarray:
    return np.array(mask_img.convert("L"))


def _build_prompt(ref: dict, escalate: str | None = None,
                  with_structure: bool = False) -> str:
    p = EDIT_PROMPT_TMPL.format(
        structure_note=(STRUCTURE_NOTE if with_structure else ""),
        dtype=ref.get("defect_type", "defect"),
        appearance=_f(ref, "appearance"),
        severity=ref.get("severity", 3),
        count=ref.get("count", 1),
        orientation=_f(ref, "orientation"),
        geometry=_f(ref, "geometry"),
        extent=_f(ref, "extent"),
        photometry=_f(ref, "photometry"),
        edge_profile=_f(ref, "edge_profile"),
        texture_effect=_f(ref, "texture_effect"),
        prompt_hint=_f(ref, "prompt_hint"))
    if escalate and escalate in ESCALATE_HINTS:
        p += ESCALATE_HINTS[escalate].format(
            dtype=ref.get("defect_type", "defect"),
            severity=ref.get("severity", 3))
    return p


def usable_references(cfg: Config, records: list[dict],
                      verbose: bool = True) -> list[dict]:
    """按 severity 门槛筛出可作参考的缺陷。支持按类型分别设下限。

    统一门槛对划痕不公平: 划痕是细线, VLM 给的 severity 天然偏低, 用变形的
    标准会把划痕参考全部滤掉, 导致产出里一张划痕样本都没有。
    """
    gen = cfg.generation
    base = gen.get("min_reference_severity", 3)
    by_type = gen.get("min_reference_severity_by_type") or {}
    if not base and not by_type:
        return records
    kept = []
    for r in records:
        lo = by_type.get(r.get("defect_type"), base) or 0
        if int(r.get("severity", 3) or 3) >= lo:
            kept.append(r)
    if verbose and len(kept) != len(records):
        detail = ", ".join(f"{k}≥{v}" for k, v in by_type.items())
        reporting.info(f"[filter] 参考缺陷按 severity≥{base}"
                       f"{f' ({detail})' if detail else ''} 过滤: "
                       f"{len(records)} -> {len(kept)} 条")
    return kept


def _select_reference(catalog_by_class: dict, all_records: list,
                      target_class: str, rng: random.Random,
                      allow_cross: bool, cross_ratio: float) -> dict | None:
    use_cross = allow_cross and rng.random() < cross_ratio
    pool = None
    if not use_cross and catalog_by_class.get(target_class):
        pool = catalog_by_class[target_class]
    if not pool:  # 本类没有缺陷, 或选择了跨类
        pool = all_records
    return rng.choice(pool) if pool else None


_SRC_SIZE_CACHE: dict[str, tuple[int, int] | None] = {}
_SRC_SIZE_LOCK = threading.Lock()

# 失败原因日志: 之前只 print 到终端, 一旦被滚动的进度条冲掉就永久丢失,
# 出问题只能靠翻窗口, 且翻不到的部分永远不知道原因(实测出现过 58% 失败率,
# 真正原因是中转站渠道断供, 而不是限流, 靠猜会修错方向)。
_FAIL_LOG_LOCK = threading.Lock()


def _classify_failure(msg: str) -> str:
    """把失败原因粗分类, 便于跑完后统计"多少次是哪种原因"而不用逐行翻日志。"""
    m = msg or ""
    if "model_not_found" in m or "暂无可用渠道" in m:
        return "channel_unavailable"   # 中转站没有可用渠道, 重试无意义
    if " 429" in m or "429 " in m or '"429' in m:
        return "rate_limited"          # 限流, 等待后重试有意义
    if "HTTP 503" in m:
        return "service_unavailable"
    if "HTTP 5" in m:
        return "server_error"
    if ("Timeout" in m or "超时" in m or "timed out" in m.lower()
            or "ConnectionError" in m or "连接" in m):
        return "timeout"
    if "未返回图像" in m or "返回无法解析" in m:
        return "bad_response"
    if "打开失败" in m:
        return "image_open_failed"
    if "合成异常" in m:
        return "synthesis_exception"
    return "other"


def _log_failure(cfg: Config, task_stem: str, class_name: str,
                 attempt: int | None, reason: str, detail: str) -> None:
    """失败原因落盘到 outputs/fail_log.jsonl, 与 print 并行, 不影响现有输出。"""
    log_path = cfg.resolve(cfg.output.get("root", "outputs")) / "fail_log.jsonl"
    rec = {
        "time": datetime.datetime.now().isoformat(timespec="seconds"),
        "class": class_name,
        "stem": task_stem,
        "attempt": attempt,
        "reason": reason,
        "detail": detail[:500],
    }
    with _FAIL_LOG_LOCK:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def _ref_source_size(cfg: Config, rec: dict) -> tuple[int, int] | None:
    """参考缺陷所在原图的尺寸, 用于把 bbox 换算成与分辨率无关的比例。

    优先用缺陷库里的 source_size(编目时写入, 无需持有原图); 缺该字段时才按需
    打开原图读取并缓存。多人协作时只分发缺陷库不分发"有缺陷"数据集, 因此
    source_size 是保证裁块尺寸正确的关键 —— 缺了它会退回按绝对像素计算,
    在分辨率不同的类别上引入最高约 1.8 倍的裁块放大。
    """
    ss = rec.get("source_size")
    if isinstance(ss, (list, tuple)) and len(ss) == 2:
        try:
            return int(ss[0]), int(ss[1])
        except (TypeError, ValueError):
            pass
    raw = str(rec.get("source_image", ""))
    if not raw:
        return None
    key = str(cfg.resolve(raw))
    with _SRC_SIZE_LOCK:
        if key in _SRC_SIZE_CACHE:
            return _SRC_SIZE_CACHE[key]
    size = None
    try:
        with Image.open(key) as im:
            size = im.size
    except Exception:
        size = None
    with _SRC_SIZE_LOCK:
        _SRC_SIZE_CACHE[key] = size
    return size


def _load_ref_crop(cfg: Config, rec: dict) -> Image.Image | None:
    """读取参考缺陷裁剪图。crop_path 支持相对路径(相对 config.yaml 所在目录),
    这样缺陷库可以跨机器分发, 不依赖建库那台机器的绝对路径。"""
    raw = rec.get("crop_path", "")
    if not raw:
        return None
    p = cfg.resolve(str(raw))
    if not p.exists():
        return None
    try:
        return Image.open(p).convert("RGB")
    except Exception:
        return None


# --------------------------- 单张合成 ---------------------------

def _synthesize_one(cfg: Config, relay: RelayClient, clean: Image.Image,
                    bottle: np.ndarray, class_name: str,
                    catalog_by_class: dict, records: list,
                    rng: random.Random, forced_ref: dict | None = None,
                    debug_dir: Path | None = None,
                    task_stem: str = ""):
    gen = cfg.generation
    max_retries = gen.get("max_retries", 3)
    feather = gen.get("feather", 10)
    best = None
    box = None
    escalate: str | None = None

    for attempt in range(max_retries):
        ref = forced_ref or _select_reference(
            catalog_by_class, records, class_name, rng,
            gen.get("allow_cross_class", True),
            gen.get("cross_class_ratio", 0.35))
        if ref is None:
            return None

        # --- 问题1: 按参考缺陷 bbox 自适应裁块尺寸(支持长方形) ---
        if box is None:
            if gen.get("adaptive_patch", True):
                ps = mask_utils.adaptive_patch_size(
                    ref.get("bbox"), gen.get("patch_size", 768),
                    gen.get("max_patch_size", 1536),
                    gen.get("adaptive_patch_margin", 1.3), clean.size,
                    ref_img_size=_ref_source_size(cfg, ref))
            else:
                base = gen.get("patch_size", 768)
                ps = (min(base, clean.size[0]), min(base, clean.size[1]))
            box = mask_utils.pick_patch_box(
                bottle, ps, rng,
                min_bottle_cover=gen.get("min_bottle_cover", 0.55))
        orig_patch = mask_utils.crop(clean, box)

        ref_img = _load_ref_crop(cfg, ref)
        ref_imgs = [ref_img] if ref_img else []

        # 结构参考图: 只含褶皱几何, 剥离源瓶的颜色/图案/高光, 避免过曝亮边被照抄
        struct_img = None
        if ref_img is not None and gen.get("use_structure_reference", True):
            try:
                struct_img = structure_ref.defect_structure_map(
                    ref_img,
                    sigma_small=gen.get("structure_sigma_small", 6.0),
                    sigma_large=gen.get("structure_sigma_large", 40.0),
                    gain=gen.get("structure_gain", 3.0))
                ref_imgs.append(struct_img)
            except Exception as e:
                reporting.warn(f"[warn] 结构图生成失败, 退回仅用彩色参考: {e}")
                struct_img = None

        prompt = _build_prompt(ref, escalate, with_structure=struct_img is not None)

        edit_mask = None
        if cfg.models.get("edit_backend") == "openai_images":
            edit_mask = mask_utils.openai_edit_mask(
                orig_patch.width, orig_patch.height)

        try:
            raw_patch = relay.edit_image(prompt, orig_patch,
                                         references=ref_imgs, mask=edit_mask)
        except Exception as e:
            msg = str(e)
            reason = _classify_failure(msg)
            reporting.warn(f"[warn] 编辑失败(尝试{attempt+1}, {reason}): {e}")
            _log_failure(cfg, task_stem or "", class_name, attempt + 1, reason, msg)
            box = None  # 下一轮换位置
            continue

        # --- 问题3: 先做光度对齐, 再差分, 避免全局漂移被当成缺陷 ---
        edited_patch = raw_patch
        if gen.get("photometric_align", True):
            edited_patch = mask_utils.align_photometry(raw_patch, orig_patch)

        patch_mask, mask_info = mask_utils.diff_defect_mask(
            orig_patch, edited_patch,
            thresh=gen.get("diff_threshold", 14),
            max_area_ratio=gen.get("max_defect_area_ratio", 0.85),
            return_info=True)

        # 先羽化(模糊会外扩), 再施加约束, 顺序不能颠倒否则约束会被打穿
        alpha = mask_utils.feather_alpha(patch_mask, feather)
        # 约束: 只落在瓶身内 + 裁块边界前归零(消除"瓶身上下不一样"的断层)
        alpha = mask_utils.constrain_mask(
            alpha, bottle, box,
            border_fade_ratio=gen.get("border_fade_ratio", 0.08),
            bottle_erode=gen.get("bottle_erode", 6))
        # 融合用软 alpha, 标注用二值掩膜(避免过渡带虚增 bbox)
        label_mask = mask_utils.binarize_mask(
            alpha, gen.get("mask_binarize_thresh", 128))

        if mask_utils.changed_ratio(label_mask) < gen.get("min_defect_area_ratio", 0.0005):
            # 掩膜为空有两种截然相反的原因, 必须分开处理, 否则重试方向会反:
            #   a) 模型确实没怎么动图 -> 要求做强
            #   b) 模型整块重绘, 差分连成一片被面积上限吃掉 -> 要求收敛改动范围
            # 旧实现一律按 (a) 追加"要更深更明显", 反而把 (b) 推得更糟,
            # 实测出现过 a1 0.063 -> a2 0.180 -> a3 0.002 的恶化链, 三次全废。
            if mask_info["over_thresh_ratio"] >= 0.35:
                escalate = "over_repaint"
                reporting.info(f"[info] 掩膜为空但模型改动很大(超阈值像素"
                               f"{mask_info['over_thresh_ratio']:.2f}), "
                               f"下轮要求收敛改动范围")
            else:
                escalate = "severity_match"
            continue

        gain = gen.get("defect_gain", 1.0)
        if gen.get("severity_adaptive_gain", False):
            sev = int(ref.get("severity", 3) or 3)
            gain *= 1.0 + 0.12 * max(0, sev - 3)
        # 融合用软 alpha(feather=0, 因为已在上面羽化过)
        result = mask_utils.composite(
            clean, edited_patch, alpha, box,
            mode=gen.get("blend_mode", "feather"), feather=0, gain=gain)

        # 落盘/校验用二值标注掩膜
        full_mask_img = mask_utils.full_mask_from_patch(clean.size, label_mask, box)
        result_patch = mask_utils.crop(result, box)

        # --- 问题2: 参考对比式多维质检(送全分辨率裁块) ---
        # 硬约束校验用软 alpha 的覆盖范围: 羽化过渡带属于"合法被修改区",
        # 若用二值掩膜会把过渡带误判成"掩膜外变化"
        alpha_full = _to_arr(mask_utils.full_mask_from_patch(
            clean.size, (alpha > 0).astype(np.uint8) * 255, box))
        qc = quality_check.check(
            cfg, relay, clean, result, alpha_full,
            ref=ref, orig_patch=orig_patch, result_patch=result_patch,
            ref_crop=ref_img)

        bb = mask_utils.mask_bbox(label_mask)
        meta = {
            "defect_type": ref.get("defect_type"),
            "defect_location": ref.get("location"),
            "severity": ref.get("severity"),
            "reference_class": ref.get("class_name"),
            "reference_entry": ref.get("entry_id"),
            "cross_class": ref.get("class_name") != class_name,
            "patch_box": [box.x, box.y, box.w, box.h],
            "defect_bbox_global": ([box.x + bb.x, box.y + bb.y, bb.w, bb.h]
                                   if bb else None),
            "gain": round(gain, 3),
            "qc": qc.to_dict(),
            "attempt": attempt + 1,
            # 差分各环节占比: 用于区分"模型没画"与"整块重绘被面积上限吃掉"
            "mask_info": mask_info,
            "mask_ratio_final": round(float(mask_utils.changed_ratio(label_mask)), 4),
        }

        # --- 问题8: debug 中间产物, 便于归因(模型弱? mask截断? 融合削弱?) ---
        if debug_dir is not None:
            debug_dir.mkdir(parents=True, exist_ok=True)
            tag = f"a{attempt+1}"
            orig_patch.save(debug_dir / f"{tag}_1_orig_patch.png")
            raw_patch.save(debug_dir / f"{tag}_2_model_raw.png")
            edited_patch.save(debug_dir / f"{tag}_3_aligned.png")
            Image.fromarray(alpha).save(debug_dir / f"{tag}_4a_alpha_soft.png")
            Image.fromarray(label_mask).save(debug_dir / f"{tag}_4b_mask_label.png")
            result_patch.save(debug_dir / f"{tag}_5_result_patch.png")
            if ref_img is not None:
                ref_img.save(debug_dir / f"{tag}_0a_reference.png")
            if struct_img is not None:
                struct_img.save(debug_dir / f"{tag}_0b_structure.png")
            (debug_dir / f"{tag}_meta.json").write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

        if qc.passed:
            return result, full_mask_img, meta
        if best is None or qc.realism > best[2]["qc"]["realism"]:
            best = (result, full_mask_img, meta)

        # --- 问题4: 分级重试, 针对最弱维度强化; 只有形状/类型问题才换位置 ---
        escalate = qc.weakest or None
        if escalate in ("type_match", "shape_fidelity") and forced_ref is None:
            box = None  # 换个位置重试

    # 全部未通过: 按配置决定是否兜底保留
    if best is None:
        return None
    keep = cfg.qc.get("keep_best_effort", False)
    margin = cfg.qc.get("best_effort_margin", 1.0)
    if keep and best[2]["qc"]["realism"] >= cfg.qc.get("min_realism_score", 7) - margin:
        best[2]["accepted_as"] = "best_effort"
        return best
    best[2]["accepted_as"] = "rejected"
    return ("REJECTED", best[0], best[1], best[2])


# --------------------------- 任务编排(问题5/6/10) ---------------------------

@dataclass
class Task:
    class_name: str
    clean_path: Path
    index: int
    forced_ref: dict | None = None
    stem: str = ""
    # 参与 RNG 播种但不影响文件命名。用于"同一张干净图 + 同一条参考"
    # 需要重新挑一次裁块位置的场景(人工驳回样本重生成、定向补充素材)。
    # 为空时播种字符串与改造前逐字相同, 不影响任何既有任务的可复现性。
    salt: str = ""


def _load_done(ann_path: Path) -> set[str]:
    """读取已有标注, 构建已完成集合(问题6: 断点续跑)。"""
    done: set[str] = set()
    if not ann_path.exists():
        return done
    for line in ann_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError:
            continue
        gi = r.get("generated_image")
        if gi:
            done.add(Path(gi).stem)
    return done


def _run_tasks(cfg: Config, tasks: list[Task], resume: bool = True,
               cancel: threading.Event | None = None,
               img_dir_override: Path | None = None,
               rej_dir_override: Path | None = None,
               ann_path_override: Path | None = None):
    """统一的并发执行器, generate 与 gen-ref 共用(问题10: 消除重复逻辑)。

    cancel: 置位后不再启动新任务, 已在途的任务跑完并正常落盘。刻意不去强杀
    线程, 因为 annotations.jsonl 是追加写的, 中途打断会留下半行 JSON, 下次
    断点续跑解析到那一行就会丢掉整批已完成记录。

    *_override: 让"驳回样本重生成""定向补充"这类规划器落盘到不同目录
    (outputs/regenerated/ 等), 不与主批次的产物、标注混在一起。不传时
    行为与改造前完全一致, 因此主批次生成不受影响。
    """
    relay = RelayClient(cfg)
    records = load_catalog(cfg)
    if not records:
        raise RuntimeError("缺陷库为空, 请先成功运行 build-catalog")
    _warn_legacy(records)

    # 过滤过弱的参考缺陷(severity 太低的不适合做复刻标准)
    records = usable_references(cfg, records) or records

    catalog_by_class: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        catalog_by_class[r["class_name"]].append(r)

    gen = cfg.generation
    img_dir = img_dir_override or cfg.out_path("images")
    mask_dir = cfg.out_path("masks")
    rej_dir = rej_dir_override or cfg.resolve(
        cfg.output.get("rejected", "outputs/rejected"))
    ann_path = ann_path_override or cfg.out_path("annotations")
    img_dir.mkdir(parents=True, exist_ok=True)
    ann_path.parent.mkdir(parents=True, exist_ok=True)
    debug_on = gen.get("debug", False)
    debug_root = cfg.resolve(cfg.output.get("debug", "outputs/debug"))

    done = _load_done(ann_path) if resume else set()
    pending = [t for t in tasks if t.stem not in done]
    skipped = len(tasks) - len(pending)
    reporting.event("plan", total=len(tasks), skipped=skipped,
                    pending=len(pending))
    if skipped:
        reporting.info(f"[resume] 跳过已完成 {skipped} 项, 待生成 {len(pending)} 项")
    if not pending:
        reporting.info("[done] 没有待生成的任务")
        reporting.event("finished", status="nothing_to_do", stats={
            "ok": 0, "best_effort": 0, "rejected": 0,
            "failed": 0, "cancelled": 0})
        return

    ann_lock = threading.Lock()
    ann_f = open(ann_path, "a", encoding="utf-8")
    stats = {"ok": 0, "best_effort": 0, "rejected": 0, "failed": 0,
             "cancelled": 0}

    def work(t: Task):
        # 尚未启动的任务在这里直接退出, 不发起任何 API 调用(不烧额度)
        if cancel is not None and cancel.is_set():
            return t, None, CANCELLED
        seed = gen.get("seed", 42)
        seed_str = f"{seed}|{t.class_name}|{t.clean_path.name}|{t.index}"
        if t.salt:
            seed_str += f"|{t.salt}"
        rng = random.Random(seed_str)
        try:
            clean = Image.open(t.clean_path).convert("RGB")
        except Exception as e:
            return t, None, f"打开失败: {e}"
        bottle = mask_utils.segment_bottle(
            clean, roi=(cfg.data.get("bottle_roi") or {}).get(t.class_name))
        dbg = (debug_root / _safe(t.class_name) / t.stem) if debug_on else None
        try:
            res = _synthesize_one(cfg, relay, clean, bottle, t.class_name,
                                  catalog_by_class, records, rng,
                                  forced_ref=t.forced_ref, debug_dir=dbg,
                                  task_stem=t.stem)
        except Exception as e:
            return t, None, f"合成异常: {e}"
        return t, res, None

    workers = max(1, int(cfg.api.get("max_workers", 3)))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(work, t) for t in pending]
        done_count = 0
        for fut in reporting.track(as_completed(futs), total=len(futs),
                                   desc="生成"):
            t, res, err = fut.result()
            if err == CANCELLED:
                stats["cancelled"] += 1
                continue
            done_count += 1
            reporting.progress_tick(done_count, len(futs))
            if err or res is None:
                stats["failed"] += 1
                if err:
                    reason = _classify_failure(err)
                    reporting.warn(f"[warn] {t.clean_path.name} ({reason}): {err}")
                    _log_failure(cfg, t.stem, t.class_name, None, reason, err)
                else:
                    # _synthesize_one 内部三次重试均未通过, 逐次失败已在
                    # 编辑调用处落过日志, 这里补一条"任务彻底失败"的汇总记录
                    _log_failure(cfg, t.stem, t.class_name, None,
                                "exhausted_retries", "三次重试后仍未获得合格/兜底结果")
                continue

            rejected = isinstance(res, tuple) and len(res) == 4 and res[0] == "REJECTED"
            if rejected:
                _, result_img, full_mask, meta = res
                out_dir = rej_dir / _safe(t.class_name)
                stats["rejected"] += 1
            else:
                result_img, full_mask, meta = res
                out_dir = img_dir / _safe(t.class_name)
                stats["best_effort" if meta.get("accepted_as") == "best_effort"
                      else "ok"] += 1

            out_dir.mkdir(parents=True, exist_ok=True)
            out_img = out_dir / f"{t.stem}.png"
            result_img.save(out_img)
            # 驳回样本不落掩膜: 它们不进训练集, 掩膜没有用途, 只会让 rejected
            # 目录里图与掩膜混在一起, 人工复核时干扰视线。
            if rejected:
                out_mask = None
            else:
                cls_mask_dir = mask_dir / _safe(t.class_name)
                cls_mask_dir.mkdir(parents=True, exist_ok=True)
                out_mask = cls_mask_dir / f"{t.stem}_mask.png"
                full_mask.save(out_mask)

            meta.update({
                "class": t.class_name,
                "clean_source": str(t.clean_path),
                "generated_image": str(out_img),
                "mask": str(out_mask) if out_mask else "",
            })
            with ann_lock:
                ann_f.write(json.dumps(meta, ensure_ascii=False) + "\n")
                ann_f.flush()
            reporting.event(
                "task", stem=t.stem, cls=t.class_name,
                verdict=("rejected" if rejected
                         else meta.get("accepted_as") or "ok"),
                realism=meta.get("qc", {}).get("realism"),
                attempt=meta.get("attempt"))

    ann_f.close()
    reporting.info(f"\n[done] 合格={stats['ok']} 兜底={stats['best_effort']} "
                   f"驳回={stats['rejected']} 失败={stats['failed']}")
    reporting.info(f"       标注: {ann_path}")
    if stats["cancelled"]:
        reporting.info(f"       已取消未开始的任务: {stats['cancelled']} 项"
                       f"(可重跑同样命令续跑)")
    if stats["rejected"]:
        reporting.info(f"       驳回样本另存于: {rej_dir} (不混入训练集)")
    if stats["failed"]:
        fail_log = cfg.resolve(cfg.output.get("root", "outputs")) / "fail_log.jsonl"
        reporting.info(f"       失败原因日志: {fail_log}")
        reporting.info(f"       (用 `run.py fail-summary` 查看本次失败原因分类统计)")
    reporting.event(
        "finished",
        status=("cancelled" if cancel is not None and cancel.is_set()
                else "completed"),
        stats=dict(stats))


def _warn_legacy(records: list[dict]):
    """问题9: 提示缺陷库中的旧格式条目。"""
    legacy = [r for r in records if "severity" not in r]
    if legacy:
        reporting.warn(
            f"[warn] 缺陷库中有 {len(legacy)}/{len(records)} 条为旧格式"
            f"(缺 severity 等细化字段), 建议 build-catalog --overwrite 重建")


# --------------------------- 对外入口 ---------------------------

def generate(cfg: Config, limit_per_class: int | None = None,
             only_classes: list[str] | None = None, resume: bool = True,
             cancel: threading.Event | None = None):
    clean_images = scan_class_images(cfg, cfg.clean_root())
    if only_classes:
        clean_images = {k: v for k, v in clean_images.items() if k in only_classes}
    num_per = cfg.generation.get("num_per_clean", 2)

    tasks: list[Task] = []
    for class_name, paths in clean_images.items():
        if limit_per_class:
            paths = paths[:limit_per_class]
        for p in paths:
            for k in range(num_per):
                tasks.append(Task(class_name, p, k,
                                  stem=f"{_safe(p.stem)}__d{k}"))
    reporting.info(f"[plan] 共 {len(tasks)} 个生成任务, 覆盖 {len(clean_images)} 个类别")
    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)


def generate_sweep(cfg: Config, classes: list[str], resume: bool = True,
                   shuffle: bool = False, max_refs: int | None = None,
                   cancel: threading.Event | None = None):
    """每条参考缺陷只用一次: 一张干净图配一条参考, 按给定类别顺序铺开。

    与 generate() 的区别: generate() 是"以干净图为主体, 每张随机抽参考",
    同一条参考会被反复使用; 这里是"以参考缺陷为主体", 保证 122 条参考各用一次,
    覆盖全部缺陷形态且不重复。

    铺开规则: 按 classes 给出的顺序, 用完一个类别的干净图就溢出到下一个类别。
    例: 参考 122 条, classes=[5.1, 5.2], 5.1 有 107 张干净图
        -> 5.1 生成 107 张(用掉参考 1~107), 5.2 生成 15 张(用掉参考 108~122)
    参考用尽即停止, 后面的类别不再产生任务。
    """
    records = load_catalog(cfg)
    if not records:
        raise RuntimeError("缺陷库为空, 请先成功运行 build-catalog")

    # 与 _run_tasks 内部一致的参考过滤, 这里要先过滤才能正确分配
    records = usable_references(cfg, records, verbose=False)
    if shuffle:
        random.Random(cfg.generation.get("seed", 42)).shuffle(records)
    if max_refs:
        records = records[:max_refs]

    clean_images = scan_class_images(cfg, cfg.clean_root())
    tasks: list[Task] = []
    cursor = 0
    plan: list[tuple[str, int]] = []
    for class_name in classes:
        if cursor >= len(records):
            break
        paths = clean_images.get(class_name)
        if not paths:
            reporting.warn(f"[warn] 类别无干净图, 跳过: {class_name}")
            continue
        take = min(len(paths), len(records) - cursor)
        for i in range(take):
            ref = records[cursor + i]
            tasks.append(Task(
                class_name, paths[i], 0, forced_ref=ref,
                stem=f"{_safe(paths[i].stem)}__ref_{_safe(ref['entry_id'])}"))
        cursor += take
        plan.append((class_name, take))

    reporting.info(f"[plan] 可用参考缺陷 {len(records)} 条, "
                   f"每条只用一次, 共 {len(tasks)} 个任务")
    for class_name, take in plan:
        reporting.info(f"        {class_name}: {take} 张")
    left = len(records) - cursor
    if left > 0:
        reporting.warn(f"[warn] 还有 {left} 条参考没分配到干净图, "
                       f"请在 --classes 后面追加更多类别")
    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)


def generate_target(cfg: Config, classes: list[str], count: int,
                    resume: bool = True,
                    cancel: threading.Event | None = None) -> None:
    """给指定类别各生成固定数量的样本, 并保证覆盖缺陷库里每一条可用参考。

    与 generate()/generate_sweep() 的区别:
      generate()       以干净图为主体, 数量由 num_per_clean 决定, 参考随机重复选取
      generate_sweep() 以参考为主体, 每条参考只用一次, 数量由"干净图数量"上限决定
      generate_target()由目标数量 count 决定任务数, 参考按顺序循环使用,
                       count >= 参考总数时保证每条参考至少用一次(足量还会循环第二轮)

    用于"每类固定生成 N 张"的分工场景: 参考库共用, 每个类各自独立循环一遍,
    因此对同一批参考库, 12 个类各跑一次即可让每条参考在每个类别上都出现过。
    """
    records = load_catalog(cfg)
    if not records:
        raise RuntimeError("缺陷库为空, 请先成功运行 build-catalog")
    usable = usable_references(cfg, records)
    if not usable:
        raise RuntimeError("过滤后没有可用参考, 检查 min_reference_severity 配置")

    clean_images = scan_class_images(cfg, cfg.clean_root())
    n_ref = len(usable)
    tasks: list[Task] = []

    for class_name in classes:
        paths = clean_images.get(class_name)
        if not paths:
            reporting.warn(f"[warn] 类别无干净图, 跳过: {class_name}")
            continue
        n_clean = len(paths)
        full_cycles, remainder = divmod(count, n_ref)
        cover_msg = (f"完整覆盖 {full_cycles} 轮 + 前 {remainder} 条"
                    if remainder else f"完整覆盖 {full_cycles} 轮")
        if count < n_ref:
            cover_msg = f"仅覆盖前 {count}/{n_ref} 条, 覆盖不全!"
        reporting.info(f"[plan] {class_name}: 目标 {count} 张, 参考库 {n_ref} 条 "
                       f"({cover_msg}), 干净图 {n_clean} 张(循环使用)")
        for i in range(count):
            ref = usable[i % n_ref]
            clean = paths[i % n_clean]
            stem = (f"{_safe(class_name)}__{_safe(clean.stem)}__t{i}"
                    f"__ref_{_safe(ref['entry_id'])}")
            tasks.append(Task(class_name, clean, i, forced_ref=ref, stem=stem))

    reporting.info(f"\n[plan] 共 {len(tasks)} 个任务")
    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)


def generate_with_reference(cfg: Config, reference_entry: str,
                            classes: list[str], per_class: int = 1,
                            resume: bool = True,
                            cancel: threading.Event | None = None):
    """定向实验: 固定用某条参考缺陷, 在指定类别各随机抽 per_class 张干净图生成。"""
    records = load_catalog(cfg)
    ref = next((r for r in records if r["entry_id"] == reference_entry), None)
    if ref is None:  # 宽松匹配: 允许不带末尾 __0 的前缀写法
        cand = [r for r in records if r["entry_id"].startswith(reference_entry)]
        ref = cand[0] if cand else None
    if ref is None:
        raise ValueError(f"缺陷库中找不到参考条目: {reference_entry}\n"
                         f"可用示例: {[r['entry_id'] for r in records[:5]]} ...")
    reporting.info(f"[参考缺陷] {ref['entry_id']} | 类型={ref['defect_type']} "
                   f"| severity={ref.get('severity','?')} | "
                   f"{ref.get('appearance','')}")

    clean_images = scan_class_images(cfg, cfg.clean_root())
    rng = random.Random(cfg.generation.get("seed", 42))
    tasks: list[Task] = []
    for class_name in classes:
        paths = clean_images.get(class_name)
        if not paths:
            reporting.warn(f"[warn] 类别无干净图: {class_name}")
            continue
        for p in rng.sample(paths, min(per_class, len(paths))):
            tasks.append(Task(class_name, p, 0, forced_ref=ref,
                              stem=f"{_safe(p.stem)}__ref_{_safe(ref['entry_id'])}"))
    reporting.info(f"[plan] 共 {len(tasks)} 个定向生成任务")
    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)


# --------------------------- 驳回样本重生成 ---------------------------

def _next_round_suffix(out_dir: Path, original_stem: str) -> str:
    """扫已存在的 {original_stem}__r*.png, 返回下一个可用轮次后缀。

    从 __r2 开始(__r1 省略不用, 避免与"第一次生成"混淆); 找不到已有轮次时
    直接给 __r2。轮次号必须探测而不能固定, 否则同一张驳回图第二次重跑会
    覆盖第一次重生成的结果, 人工没法对比多轮效果。
    """
    existing = set()
    if out_dir.exists():
        prefix = f"{original_stem}__r"
        for p in out_dir.glob(f"{prefix}*.png"):
            tail = p.stem[len(prefix):]
            if tail.isdigit():
                existing.add(int(tail))
    n = 2
    while n in existing:
        n += 1
    return f"__r{n}"


def plan_regenerate_rejected(
        cfg: Config, directories: list, reroll_ref: bool = False,
        catalog_records: list[dict] | None = None):
    """从人工驳回样本目录反查上下文, 规划出重生成任务列表。

    只负责"规划", 不落盘不调用 API —— dry-run 与真跑共用这一份规划结果,
    保证 --dry-run 打印的清单与真正执行的任务完全一致。

    返回 (tasks, scan_report, skipped): skipped 是反查成功但目标产物已存在
    的任务(视为已经重生成过, 断点续跑语义与其它规划器一致)。
    """
    from . import rejection

    records = catalog_records if catalog_records is not None else load_catalog(cfg)
    if not records:
        raise RuntimeError("缺陷库为空, 请先成功运行 build-catalog")
    by_class: dict[str, list[dict]] = defaultdict(list)
    for r in records:
        by_class[r["class_name"]].append(r)

    scan_report = rejection.scan_rejected_dirs(cfg, directories, records)

    regen_root = cfg.resolve(cfg.output.get("regenerated", "outputs/regenerated"))
    rng = random.Random(cfg.generation.get("seed", 42))

    tasks: list[Task] = []
    skipped: list = []
    for item in scan_report.resolved:
        ref = item.ref_record
        if reroll_ref:
            pool = by_class.get(item.class_name) or records
            # 排除掉原参考, 否则"换一条"有较大概率抽回同一条(概率随库内
            # 同类别条目数减少而升高), 与用户意图(换个思路重试)相悖
            alt_pool = [r for r in pool if r["entry_id"] != item.ref_entry_id]
            ref = rng.choice(alt_pool or pool)

        out_dir = regen_root / _safe(item.class_name)
        suffix = _next_round_suffix(out_dir, item.original_stem)
        stem = f"{item.original_stem}{suffix}"

        if (out_dir / f"{stem}.png").exists():
            skipped.append(item)
            continue

        # salt 用重生成的目标 stem 本身: 同一张驳回图多轮重生成时,
        # stem 因轮次号不同而不同, 天然保证每轮换到不同的裁块位置。
        tasks.append(Task(
            item.class_name, item.clean_path, item.index,
            forced_ref=ref, stem=stem, salt=stem))
    return tasks, scan_report, skipped


def regenerate_rejected(
        cfg: Config, directories: list, reroll_ref: bool = False,
        cancel: threading.Event | None = None) -> dict:
    """重新生成人工驳回样本, 落盘到 outputs/regenerated/ 供人工二次 review。

    刻意不写进 outputs/generated/: 这批本来就是被挑出来的不合格样本,
    重新生成后仍应该先让人看一眼, 由用户自行决定要不要合并进训练集
    (需求明确不要程序代管合并)。

    产物目录与标注文件都换成 regenerated 下的独立位置(经 _run_tasks 的
    *_override 参数), 不与主批次的 outputs/generated、
    outputs/annotations.jsonl 混在一起, 也就不会干扰主批次的断点续跑。
    """
    tasks, scan_report, skipped = plan_regenerate_rejected(
        cfg, directories, reroll_ref=reroll_ref)

    if scan_report.problems:
        for p in scan_report.problems:
            reporting.warn(f"[warn] {p}")
    reporting.info(
        f"[plan] 扫描 {scan_report.scanned_files} 个文件, "
        f"反查成功 {scan_report.ok_count}, 反查失败 {scan_report.problem_count}, "
        f"已重生成过跳过 {len(skipped)}, 待重生成 {len(tasks)}")

    if not tasks:
        reporting.info("[done] 没有待重生成的任务")
        return {"planned": 0, "skipped": len(skipped),
                "problems": len(scan_report.problems)}

    regen_root = cfg.resolve(cfg.output.get("regenerated", "outputs/regenerated"))
    # 重生成过程中若再次判定不合格, 也落在 regenerated 下便于对比,
    # 不能混进主批次的 outputs/rejected/
    _run_tasks(cfg, tasks, resume=False, cancel=cancel,
              img_dir_override=regen_root,
              rej_dir_override=regen_root / "_rejected",
              ann_path_override=regen_root / "annotations.jsonl")

    return {"planned": len(tasks), "skipped": len(skipped),
            "problems": len(scan_report.problems)}


# --------------------------- 定向补充(按参考缺陷批量生成) ---------------------------

def plan_augment_by_references(
        cfg: Config, ref_entry_ids: list, classes: list, per_ref: int = 1,
        catalog_records: list[dict] | None = None) -> list[Task]:
    """给定一组参考缺陷条目, 在指定类别上各铺开生成 per_ref 张。

    用于需求二: 训练后发现某类褶皱识别效果差, 先用 catalog_query(按属性筛)或
    rejection.extract_reference_ids(按出问题的产出图反查)选出针对性的参考条目,
    再用这个函数批量补充。

    只负责"规划", 不落盘不调用 API。与 build-catalog / regen-rejected 保持同一
    模式: dry-run 与真跑共用同一份任务列表, 保证打印的清单和真正执行的内容一致。

    命名 {class}__{clean_stem}__aug{i}__ref_{entry_id}, 用 aug 而不是
    generate_target() 的 t 前缀, 便于事后在 outputs/generated 里靠文件名区分
    "主批次"与"定向补充"产出的样本。

    干净图分配: 用一个在"参考 x 张数"上连续推进的游标, 而不是每条参考都从第 0
    张开始 —— 后者在 per_ref=1 时会让所有参考共用同一张干净图, 补充出来的样本
    背景全都一样, 对训练几乎没有增益。
    """
    from .defect_catalog import load_catalog

    records = catalog_records if catalog_records is not None else load_catalog(cfg)
    if not records:
        raise RuntimeError("缺陷库为空, 请先成功运行 build-catalog")
    by_entry = {r["entry_id"]: r for r in records}

    missing = [eid for eid in ref_entry_ids if eid not in by_entry]
    if missing:
        tail = "..." if len(missing) > 5 else ""
        raise ValueError(f"缺陷库中找不到以下参考条目: {missing[:5]}{tail}")

    clean_images = scan_class_images(cfg, cfg.clean_root())
    tasks: list[Task] = []
    for class_name in classes:
        paths = clean_images.get(class_name)
        if not paths:
            reporting.warn(f"[warn] 类别无干净图, 跳过: {class_name}")
            continue
        n_clean = len(paths)
        cursor = 0
        for eid in ref_entry_ids:
            ref = by_entry[eid]
            for i in range(per_ref):
                clean = paths[cursor % n_clean]
                cursor += 1
                stem = (f"{_safe(class_name)}__{_safe(clean.stem)}__aug{i}"
                        f"__ref_{_safe(eid)}")
                # 干净图数量不足时会被复用; salt 取完整 stem(含 aug 序号与参考名),
                # 保证即便复用同一张干净图, RNG 播种也不同, 裁块位置不会重复
                tasks.append(Task(class_name, clean, i, forced_ref=ref,
                                  stem=stem, salt=stem))
    return tasks


def augment_by_references(
        cfg: Config, ref_entry_ids: list, classes: list, per_ref: int = 1,
        resume: bool = True, cancel: threading.Event | None = None) -> None:
    """执行定向补充。

    产物落进 outputs/generated/(主数据集), 而不是像驳回样本重生成那样另存到
    待审目录: 这批样本的目的就是直接修补训练集的检测短板, 而且质检
    (qc.passed)照常把关 —— 不合格的一样会落进 outputs/rejected/, 不会因为
    "定向补充"就放松标准。
    """
    tasks = plan_augment_by_references(cfg, ref_entry_ids, classes, per_ref)
    reporting.info(f"[plan] 定向补充: {len(ref_entry_ids)} 条参考 x "
                   f"{len(classes)} 个类别 x 每条 {per_ref} 张, "
                   f"共 {len(tasks)} 个任务")
    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)
