"""缺陷库体检: 判定逻辑与展现分离。

原本这段逻辑写在 run.py 的 cmd_inspect 里, 边算边 print, 图形界面无法复用。
这里把它提炼为 build_report() 返回结构化结果, 再由 format_lines() 渲染成与
改造前逐字相同的控制台输出。同一份判定, 两种展现。

注意: format_lines() 的输出格式是被回归测试逐行比对的(baseline/inspect.txt),
调整措辞前要同步更新基线, 否则等于悄悄改变了既有命令的行为。
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config

# 描述性字段。缺失超过 2 项视为"描述字段大量缺失"(与原实现一致)
DESCRIPTIVE_FIELDS = ("orientation", "geometry", "extent", "photometry",
                      "edge_profile", "texture_effect", "prompt_hint")

MAX_SPARSE_MISSING = 2

# 各项问题清单在控制台里只列前几条, 避免几百条 entry_id 刷屏
SAMPLE_LIMIT = 5

STATUS_OK = "ok"
STATUS_WARNING = "warning"
STATUS_ERROR = "error"


@dataclass
class CatalogReport:
    """缺陷库体检结果。字段与 cmd_inspect 原来打印的信息一一对应。"""

    catalog_path: Path
    exists: bool = False

    total: int = 0
    new_format: int = 0
    legacy_format: int = 0

    by_type: dict = field(default_factory=dict)
    by_severity: dict = field(default_factory=dict)
    by_class: dict = field(default_factory=dict)

    bad_type: list = field(default_factory=list)
    missing_crops: list = field(default_factory=list)
    missing_source_size: list = field(default_factory=list)
    sparse_fields: list = field(default_factory=list)
    oversized: list = field(default_factory=list)

    # 经 severity 门槛过滤后真正可作参考的条目数。cmd_inspect 原来不打印这个,
    # 但界面需要它 —— 用户看到的"参考库 653 条"就是这个数, 与总数 668 不同。
    usable: int = 0
    max_patch_size: int = 0

    @property
    def status(self) -> str:
        if not self.exists:
            return STATUS_ERROR
        if self.missing_crops or self.bad_type or self.total == 0:
            return STATUS_ERROR
        if self.legacy_format or self.missing_source_size or self.sparse_fields:
            return STATUS_WARNING
        return STATUS_OK

    @property
    def summary(self) -> str:
        """一句话结论, 供界面概览卡片使用。"""
        if not self.exists:
            return "缺陷库未安装"
        if self.missing_crops:
            return f"{self.total} 条, 但有 {len(self.missing_crops)} 条裁剪图缺失"
        return f"{self.total} 条, 可用参考 {self.usable} 条"


def load_records(cfg: Config) -> tuple[Path, list[dict]]:
    """返回 (缺陷库文件路径, 条目列表)。文件不存在时条目为空列表。"""
    path = cfg.out_path("catalog") / "catalog.json"
    if not path.exists():
        return path, []
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return path, []
    return path, records if isinstance(records, list) else []


def build_report(cfg: Config) -> CatalogReport:
    path, records = load_records(cfg)
    if not path.exists():
        return CatalogReport(catalog_path=path, exists=False)

    max_patch = cfg.generation.get("max_patch_size", 1536)
    report = CatalogReport(
        catalog_path=path,
        exists=True,
        total=len(records),
        max_patch_size=max_patch,
    )

    new_format = [r for r in records if "severity" in r]
    report.new_format = len(new_format)
    report.legacy_format = len(records) - len(new_format)

    # 按类型保持首次出现顺序(与原实现的 Counter 行为一致), 按类别/严重度排序
    report.by_type = dict(Counter(r.get("defect_type") for r in records))
    report.by_severity = dict(sorted(
        Counter(r.get("severity", "?") for r in records).items(),
        key=lambda kv: str(kv[0])))
    report.by_class = dict(sorted(
        Counter(r.get("class_name") for r in records).items()))

    whitelist = (cfg.get("catalog", {}) or {}).get(
        "defect_types", ["变形", "划痕"])
    report.bad_type = [r["entry_id"] for r in records
                       if r.get("defect_type") not in whitelist]
    report.missing_crops = [
        r["entry_id"] for r in records
        if not cfg.resolve(str(r.get("crop_path", ""))).exists()]
    report.missing_source_size = [
        r["entry_id"] for r in records
        if not isinstance(r.get("source_size"), (list, tuple))]
    report.sparse_fields = [
        r["entry_id"] for r in records
        if sum(1 for k in DESCRIPTIVE_FIELDS
               if not str(r.get(k, "")).strip()) > MAX_SPARSE_MISSING]

    report.oversized = [
        (r["entry_id"], r["bbox"][2], r["bbox"][3]) for r in records
        if r.get("bbox") and max(r["bbox"][2], r["bbox"][3]) > max_patch]

    report.usable = _count_usable(cfg, records)
    return report


def _count_usable(cfg: Config, records: list[dict]) -> int:
    """经 severity 门槛过滤后的可用参考数。

    刻意复用 generate.usable_references 而不是在这里重写一遍过滤规则:
    该规则对划痕有单独下限, 抄一遍就会在改配置时悄悄分叉。
    延迟导入是为了避免为了数个数就把 api_client / quality_check 一并拉进来。
    """
    try:
        from .generate import usable_references
        return len(usable_references(cfg, records, verbose=False))
    except Exception:
        return 0


def format_lines(report: CatalogReport) -> list[str]:
    """渲染为控制台文本。必须与改造前 cmd_inspect 的输出逐字一致。"""
    if not report.exists:
        return [f"缺陷库不存在: {report.catalog_path}"]

    lines = [
        f"缺陷库: {report.catalog_path}",
        f"条目总数: {report.total}",
        f"新格式(含细化字段): {report.new_format} / 旧格式: {report.legacy_format}",
        f"按类型: {report.by_type}",
        f"按严重度: {report.by_severity}",
        f"按类别: {report.by_class}",
        "",
        f"非白名单类型: {len(report.bad_type)} {report.bad_type[:SAMPLE_LIMIT]}",
        f"裁剪图缺失: {len(report.missing_crops)} "
        f"{report.missing_crops[:SAMPLE_LIMIT]}",
    ]
    if report.missing_source_size:
        lines.append(
            f"[警告] 缺 source_size 的条目: {len(report.missing_source_size)} 条 "
            f"—— 这些条目在没有'有缺陷'原图的机器上会退回按绝对像素算裁块(尺寸偏大)")
    lines.append(
        f"描述字段大量缺失: {len(report.sparse_fields)} "
        f"{report.sparse_fields[:SAMPLE_LIMIT]}")

    if report.oversized:
        lines.append("")
        lines.append(f"[提示] {len(report.oversized)} 条缺陷长边超过 "
                     f"max_patch_size, 裁块无法完整覆盖:")
        for entry_id, w, h in report.oversized[:SAMPLE_LIMIT]:
            lines.append(f"   {entry_id}  bbox={w}x{h}")
    return lines
