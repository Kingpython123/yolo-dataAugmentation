"""无缺陷样本数据集扫描。

要解决的具体问题(requirements.md FR-4.3):
改造前数据集必须放在项目上一级、目录名为全角括号的"实拍负样本（无缺陷）"。
路径写错时唯一的反馈是运行到一半冒出一句"类别无干净图", 用户无从判断是层级
错了、括号是半角、还是扩展名不匹配。这里在扫描阶段就把原因查出来并给出可执行
的建议。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from src.config import Config
from src.dataset import safe_name

# 与 config.yaml 的 data.extensions 默认值保持一致
DEFAULT_EXTENSIONS = (".bmp", ".jpg", ".jpeg", ".png")

# 全角括号是原始数据集目录名的一部分, 手工输入时极易写成半角
FULLWIDTH_PARENS = ("（", "）")


@dataclass
class ClassInfo:
    name: str
    image_count: int
    done_count: int = 0        # 已生成并记入 annotations 的样本数
    rejected_count: int = 0

    @property
    def display(self) -> str:
        return f"{self.name} ({self.image_count} 张)"


@dataclass
class DatasetReport:
    root: Path
    exists: bool = False
    classes: list = field(default_factory=list)     # list[ClassInfo]
    total_images: int = 0
    hints: list = field(default_factory=list)       # 可执行的修复建议

    @property
    def ok(self) -> bool:
        return self.exists and bool(self.classes)

    @property
    def class_names(self) -> list:
        return [c.name for c in self.classes]

    @property
    def summary(self) -> str:
        if not self.exists:
            return "数据目录不存在"
        if not self.classes:
            return "目录存在但未识别到任何类别"
        return f"{len(self.classes)} 个类别, 共 {self.total_images} 张干净图"


def scan(root: Path | str, extensions: tuple | list = DEFAULT_EXTENSIONS,
         cfg: Config | None = None) -> DatasetReport:
    """扫描无缺陷样本根目录。

    不复用 src.dataset.scan_class_images 是因为那个函数需要一个 Config, 且会
    静默丢掉"有子目录但没图片"的类别 —— 而那恰好是最需要提示用户的情形。
    """
    root = Path(root).expanduser()
    report = DatasetReport(root=root)

    if not root.exists():
        report.hints.append(f"目录不存在: {root}")
        report.hints.extend(_hints_for_missing_root(root))
        return report
    if not root.is_dir():
        report.hints.append(f"这不是一个目录: {root}")
        return report

    report.exists = True
    exts = {str(e).lower() for e in extensions}

    subdirs = sorted(p for p in root.iterdir() if p.is_dir())
    empty_dirs: list[str] = []
    other_ext: set[str] = set()

    for d in subdirs:
        images: list[Path] = []
        for p in sorted(d.rglob("*")):
            if not p.is_file():
                continue
            suffix = p.suffix.lower()
            if suffix in exts:
                images.append(p)
            elif suffix:
                other_ext.add(suffix)
        if images:
            report.classes.append(ClassInfo(name=d.name,
                                           image_count=len(images)))
        else:
            empty_dirs.append(d.name)

    report.total_images = sum(c.image_count for c in report.classes)

    if not subdirs:
        loose = [p for p in root.iterdir()
                 if p.is_file() and p.suffix.lower() in exts]
        if loose:
            report.hints.append(
                f"根目录下直接放着 {len(loose)} 张图片, 但程序按"
                f"'一个子目录 = 一个类别'的结构读取。请把图片按类别放进子目录, "
                f"例如 {root.name}/1.jpg/、{root.name}/5.1/")
        else:
            report.hints.append("目录里没有任何子目录, 请确认选的是数据集根目录")

    if empty_dirs:
        listed = ", ".join(empty_dirs[:5])
        more = f" 等 {len(empty_dirs)} 个" if len(empty_dirs) > 5 else ""
        hint = f"以下子目录里没有可识别的图片: {listed}{more}"
        if other_ext:
            hint += (f"。目录中存在这些扩展名: {', '.join(sorted(other_ext)[:6])}, "
                     f"当前只识别 {', '.join(sorted(exts))}"
                     f"(可在 config.yaml 的 data.extensions 调整)")
        report.hints.append(hint)

    if cfg is not None and report.classes:
        _fill_progress(cfg, report)
    return report


def _hints_for_missing_root(root: Path) -> list[str]:
    """路径不存在时, 猜测最可能的原因。"""
    hints: list[str] = []
    name = root.name

    # 半角/全角括号混淆: 原始数据集目录名用的是全角括号
    if "(" in name or ")" in name:
        candidate = root.with_name(
            name.replace("(", FULLWIDTH_PARENS[0]).replace(")", FULLWIDTH_PARENS[1]))
        if candidate.exists():
            hints.append(f"括号是半角。同级存在全角括号的目录: {candidate}")
        else:
            hints.append("目录名里的括号请确认是全角（）还是半角(), "
                         "原始数据集用的是全角")

    parent = root.parent
    if parent.exists():
        siblings = [p.name for p in parent.iterdir() if p.is_dir()]
        near = [s for s in siblings if _looks_similar(s, name)]
        if near:
            hints.append(f"同级下的相似目录: {', '.join(near[:5])}")
        elif siblings:
            hints.append(f"父目录 {parent} 下现有子目录: "
                         f"{', '.join(sorted(siblings)[:8])}")
    else:
        hints.append(f"上级目录也不存在: {parent}")
    return hints


def _looks_similar(a: str, b: str) -> bool:
    """粗略判断两个目录名是否只差括号形态或空白。"""
    def norm(s: str) -> str:
        for ch in "（）() 　_-":
            s = s.replace(ch, "")
        return s.lower()
    return norm(a) == norm(b)


def _fill_progress(cfg: Config, report: DatasetReport) -> None:
    """从 annotations.jsonl 统计每个类别已生成/已驳回的数量。

    与断点续跑用的是同一个文件, 因此界面上显示的"已完成"与实际会跳过的数量一致。
    """
    ann_path = cfg.resolve(cfg.output.get("annotations",
                                          "outputs/annotations.jsonl"))
    if not ann_path.exists():
        return
    done: dict[str, int] = {}
    rejected: dict[str, int] = {}
    try:
        with open(ann_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cls = rec.get("class")
                if not cls:
                    continue
                if rec.get("accepted_as") == "rejected":
                    rejected[cls] = rejected.get(cls, 0) + 1
                else:
                    done[cls] = done.get(cls, 0) + 1
    except OSError:
        return

    for info in report.classes:
        info.done_count = done.get(info.name, 0)
        info.rejected_count = rejected.get(info.name, 0)


def output_dir_for_class(cfg: Config, class_name: str) -> Path:
    """某类别的产出目录。目录名净化规则必须与生成流程一致。"""
    return cfg.out_path("images") / safe_name(class_name)
