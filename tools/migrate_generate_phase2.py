"""一次性迁移: 给 src/generate.py 加 augment_by_references() 规划器(需求二)。

策略同 migrate_generate_phase1.py: 全有或全无, 显式 utf-8 读写并保留 CRLF。

待插入的代码用三引号原样嵌入(而不是逐行拼接带转义的字符串字面量), 因为要插入的
代码里本身含有大量 f-string 与双引号, 逐行手写转义极易出错 —— 上一版就是在这里
写坏了一处引号导致脚本自身语法错误。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src" / "generate.py"

# 锚点: regenerate_rejected() 的最后一条 return。注意与函数内提前返回的
# {"planned": 0, ...} 不同, 这里是 len(tasks), 因此在文件中唯一。
ANCHOR = '''    return {"planned": len(tasks), "skipped": len(skipped),
            "problems": len(scan_report.problems)}
'''

NEW_CODE = '''

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
'''


def main() -> int:
    with TARGET.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    n = text.count(ANCHOR)
    if n != 1:
        print(f"锚点命中 {n} 次(期望 1), 未做任何修改", file=sys.stderr)
        return 1

    text = text.replace(ANCHOR, ANCHOR + NEW_CODE, 1)
    print("  ok  regenerate_rejected() 之后追加 augment_by_references()")

    if uses_crlf:
        text = text.replace("\n", "\r\n")
    with TARGET.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print(f"\n已写回 {TARGET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
