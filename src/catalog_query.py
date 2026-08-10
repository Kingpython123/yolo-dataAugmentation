"""按属性筛选缺陷库条目, 用于"定向补充某种形态的样本"(需求二方式 A)。

设计取舍
--------
不引入任何新的分类体系, 完全基于 build-catalog 已经写好的字段
(defect_type / severity / count / orientation 等自由文本)做过滤。理由:
  1. 缺陷库已实测有区分度(见 requirements.md 1.3): severity 1~5 分布齐全,
     orientation 关键词分布(斜向511/交叉367/横向288/竖纵116)彼此有区别。
  2. orientation/geometry 是 VLM 生成的自由文本, 没有枚举值, 只能用关键词
     命中而不是精确匹配 —— 这是文本字段本身的性质决定的, 不是本模块偷懒。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config

# orientation 里能稳定命中的关键词 -> 归一化后的类别名。用于统计与 CLI 提示,
# 过滤时仍按用户传入的原始关键词做包含匹配(不局限于这张表)。
ORIENTATION_KEYWORDS = ("斜向", "横向", "竖向", "纵向", "交叉", "多向", "放射")


@dataclass
class QueryFilter:
    """筛选条件, 各字段为 None 表示不限制该维度。"""

    defect_type: str | None = None          # 精确匹配, 如 "变形" / "划痕"
    severity_min: int | None = None
    severity_max: int | None = None
    count_min: int | None = None
    count_max: int | None = None
    orientation_kw: str | None = None        # 关键词包含匹配, 如 "横向"
    class_name: str | None = None            # 限定来源类别(如只要 5.1 的参考)

    def matches(self, rec: dict) -> bool:
        if self.defect_type is not None and rec.get("defect_type") != self.defect_type:
            return False
        sev = int(rec.get("severity", 3) or 3)
        if self.severity_min is not None and sev < self.severity_min:
            return False
        if self.severity_max is not None and sev > self.severity_max:
            return False
        cnt = int(rec.get("count", 1) or 1)
        if self.count_min is not None and cnt < self.count_min:
            return False
        if self.count_max is not None and cnt > self.count_max:
            return False
        if self.orientation_kw is not None:
            text = str(rec.get("orientation", "") or "")
            if self.orientation_kw not in text:
                return False
        if self.class_name is not None and rec.get("class_name") != self.class_name:
            return False
        return True


@dataclass
class QueryResult:
    matched: list = field(default_factory=list)      # list[dict], 匹配的条目
    total: int = 0                                    # 库内总条目数(筛选前)

    @property
    def count(self) -> int:
        return len(self.matched)

    def distribution(self) -> dict:
        """匹配结果的分布概览, 供 CLI 打印, 帮助用户确认筛选范围合理。"""
        from collections import Counter
        return {
            "by_type": dict(Counter(r.get("defect_type") for r in self.matched)),
            "by_severity": dict(sorted(
                Counter(r.get("severity", "?") for r in self.matched).items(),
                key=lambda kv: str(kv[0]))),
            "by_class": dict(sorted(
                Counter(r.get("class_name") for r in self.matched).items())),
        }


def parse_range(spec: str | None) -> tuple[int | None, int | None]:
    """解析形如 "4-5"、"4"、"4-" 的区间字符串, 返回 (min, max)。

    "4-" 表示只设下限、"−5"(即 "-5")这种写法歧义太大直接不支持, 要设上限
    请写 "1-5"。空字符串或 None 表示不限制。
    """
    if not spec or not spec.strip():
        return None, None
    spec = spec.strip()
    if "-" in spec and not spec.startswith("-"):
        lo_s, _, hi_s = spec.partition("-")
        lo = int(lo_s) if lo_s.strip() else None
        hi = int(hi_s) if hi_s.strip() else None
        return lo, hi
    n = int(spec)
    return n, n


def query(records: list[dict], flt: QueryFilter) -> QueryResult:
    """对已加载的缺陷库条目做筛选。不涉及 I/O, 便于单独测试。"""
    matched = [r for r in records if flt.matches(r)]
    return QueryResult(matched=matched, total=len(records))


def load_and_query(cfg: Config, flt: QueryFilter) -> QueryResult:
    from .defect_catalog import load_catalog
    records = load_catalog(cfg)
    return query(records, flt)
