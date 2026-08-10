"""从"人工驳回"或"已生成"的产出文件名反查缺陷库参考条目。

背景
----
人工 review 挑出的不合格样本没有对应的 annotations.jsonl(可能在别的机器上跑的,
也可能标注文件已经被按 review 结果重写过)。但 generate_target / generate_sweep /
generate_with_reference 产出的文件名本身就完整编码了"用哪条参考、哪张干净图"这
两个决定生成结果的关键信息, 因此可以只凭文件名做反查, 不依赖标注文件。

反查原理
--------
不做字符串逆运算: safe_name() 把全角括号 "（）" 换成半角、把 "/" 换成 "_",
这两种字符在被替换后是不可逆的(半角括号本来就可能出现在原名里), 逆着解析
文件名会有歧义。正确做法是反过来: 对缺陷库每条 entry_id 正向算一次
safe_name(entry_id), 建一张"处理后的名字 -> 原始 entry_id"的表, 文件名里的
ref 片段去查这张表, 是精确匹配、不存在歧义。

已实测: 668 条缺陷库、30 个人工驳回样本, 反查 30/30 全部成功, 索引零键冲突。

两种反查粒度
------------
scan_rejected_dirs()       完整反查: 类别 + 干净图 + 参考条目, 要求干净图仍在
                          磁盘上。用于"驳回样本重生成"(需要拿到干净图才能重跑)。
extract_reference_ids()    只反查参考条目本身, 不要求干净图存在、不区分类别。
                          用于"定向补充"的方式 C(从出问题的产出图找出用过哪些
                          参考, 只关心参考是谁, 不关心当时配的哪张干净图)。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .dataset import list_classes, safe_name

# 与 generate.py 里几种规划器产出的命名规则对应:
#   generate_target()             {class}__{clean_stem}__t{idx}__ref_{entry_id}
#   generate_sweep()/gen_ref()    {class}__{clean_stem}__ref_{entry_id}  (无 __t{idx})
# ref 片段本身可能含有多个 "__"(entry_id 自带的分隔符), 因此用非贪婪匹配锚定
# 在已知的 "__t\d+__ref_" 或 "__ref_" 标记上, 而不是靠 ref 片段本身去分割。
_WITH_INDEX_RE = re.compile(r"^(?P<head>.+?)__t(?P<idx>\d+)__ref_(?P<ref>.+)$")
_WITHOUT_INDEX_RE = re.compile(r"^(?P<head>.+?)__ref_(?P<ref>.+)$")


@dataclass
class Resolved:
    """一个驳回文件反查出的完整上下文, 足以直接重新生成。"""

    file_path: Path
    class_name: str
    clean_stem: str
    clean_path: Path
    index: int                 # 原 __t{idx}, 无该段时为 0
    original_stem: str         # 反查出的原始任务 stem(不含扩展名), 用于推导轮次
    ref_entry_id: str
    ref_record: dict


@dataclass
class ScanReport:
    """一批目录的反查结果。反查失败的单独列出, 不会让整体反查中断。"""

    resolved: list[Resolved] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # 人类可读的失败原因
    scanned_dirs: list[Path] = field(default_factory=list)
    scanned_files: int = 0

    @property
    def ok_count(self) -> int:
        return len(self.resolved)

    @property
    def problem_count(self) -> int:
        return len(self.problems)


@dataclass
class ReferenceIdReport:
    """只反查参考条目本身的结果(方式 C 用, 不涉及干净图/类别)。"""

    entry_ids: set = field(default_factory=set)     # 去重后的 entry_id 集合
    per_file: dict = field(default_factory=dict)    # 文件路径 -> entry_id
    problems: list = field(default_factory=list)
    scanned_dirs: list = field(default_factory=list)
    scanned_files: int = 0


def _catalog_index(records: list[dict]) -> dict[str, dict]:
    """safe_name(entry_id) -> 记录本体。"""
    index: dict[str, dict] = {}
    for rec in records:
        key = safe_name(str(rec.get("entry_id", "")))
        if key:
            index[key] = rec
    return index


def _match_stem(stem: str) -> tuple[str, int, str] | None:
    """解析一个产出文件的 stem, 返回 (head, idx, ref_safe); 不匹配返回 None。

    head = safe(class) + "__" + safe(clean_stem)
    """
    m = _WITH_INDEX_RE.match(stem)
    if m:
        return m["head"], int(m["idx"]), m["ref"]
    m = _WITHOUT_INDEX_RE.match(stem)
    if m:
        return m["head"], 0, m["ref"]
    return None


def _split_head(head: str, known_classes: list[str]) -> tuple[str, str] | None:
    """把 head 拆成 (类别名, 干净图 stem)。

    按类别名长度从长到短逐个试前缀匹配, 避免 "1.jpg" 这种短类别名误吞
    实际是 "1.jpg2xxx" 之类更长类别名的前缀(本项目类别名没有这种情况,
    但按长度排序不增加成本, 顺手做对)。
    """
    for cls in known_classes:
        prefix = safe_name(cls) + "__"
        if head.startswith(prefix):
            return cls, head[len(prefix):]
    return None


def scan_rejected_dirs(cfg: Config, directories: list[Path | str],
                       catalog_records: list[dict] | None = None,
                       pattern: str = "*.png") -> ScanReport:
    """反查一批"人工驳回样本"目录里的每个文件, 得到重新生成所需的完整上下文。

    directories  可以传多个目录(需求上明确要支持多目录, 当前 CLI 默认只给一个)。
    catalog_records 允许外部传入已加载的缺陷库, 避免重复读盘; 缺省则自行加载。
    """
    from .defect_catalog import load_catalog

    records = catalog_records if catalog_records is not None else load_catalog(cfg)
    ref_index = _catalog_index(records)

    clean_root = cfg.clean_root()
    known_classes = sorted(list_classes(cfg, clean_root), key=len, reverse=True)
    exts = [e.lower() for e in cfg.data.get("extensions", [".bmp", ".jpg", ".jpeg", ".png"])]

    report = ScanReport()
    for raw_dir in directories:
        d = Path(raw_dir)
        if not d.is_absolute():
            d = cfg.resolve(str(raw_dir))
        report.scanned_dirs.append(d)
        if not d.is_dir():
            report.problems.append(f"目录不存在: {d}")
            continue

        files = sorted(d.glob(pattern))
        report.scanned_files += len(files)
        for f in files:
            parsed = _match_stem(f.stem)
            if parsed is None:
                report.problems.append(f"文件名不符合命名规则, 跳过: {f}")
                continue
            head, idx, ref_safe = parsed

            split = _split_head(head, known_classes)
            if split is None:
                report.problems.append(
                    f"无法从文件名识别出已知类别: {f} "
                    f"(数据集当前有 {len(known_classes)} 个类别, "
                    f"clean_root={clean_root})")
                continue
            class_name, clean_stem = split

            ref_record = ref_index.get(ref_safe)
            if ref_record is None:
                report.problems.append(
                    f"缺陷库中找不到参考条目 '{ref_safe}': {f}")
                continue

            clean_path = None
            for ext in exts:
                candidate = clean_root / class_name / f"{clean_stem}{ext}"
                if candidate.exists():
                    clean_path = candidate
                    break
            if clean_path is None:
                report.problems.append(
                    f"找不到原始干净图 {class_name}/{clean_stem}.*: {f}")
                continue

            report.resolved.append(Resolved(
                file_path=f,
                class_name=class_name,
                clean_stem=clean_stem,
                clean_path=clean_path,
                index=idx,
                original_stem=f.stem,
                ref_entry_id=ref_record["entry_id"],
                ref_record=ref_record,
            ))
    return report


def extract_reference_ids(directories: list[Path | str],
                          catalog_records: list[dict],
                          pattern: str = "*.png") -> ReferenceIdReport:
    """只从文件名反查参考条目本身, 不要求干净图存在、不解析类别。

    用于需求二的方式 C: "这批已生成的样本导致了误检", 只需要知道它们当时用了
    哪些参考缺陷, 不关心配的是哪张干净图 —— 因此比 scan_rejected_dirs() 更松,
    不会因为干净图后来被移动/删除而反查失败。
    """
    ref_index = _catalog_index(catalog_records)
    report = ReferenceIdReport()

    for raw_dir in directories:
        d = Path(raw_dir)
        report.scanned_dirs.append(d)
        if not d.is_dir():
            report.problems.append(f"目录不存在: {d}")
            continue
        files = sorted(d.glob(pattern))
        report.scanned_files += len(files)
        for f in files:
            parsed = _match_stem(f.stem)
            if parsed is None:
                report.problems.append(f"文件名不符合命名规则, 跳过: {f}")
                continue
            _head, _idx, ref_safe = parsed
            ref_record = ref_index.get(ref_safe)
            if ref_record is None:
                report.problems.append(
                    f"缺陷库中找不到参考条目 '{ref_safe}': {f}")
                continue
            entry_id = ref_record["entry_id"]
            report.entry_ids.add(entry_id)
            report.per_file[str(f)] = entry_id
    return report
