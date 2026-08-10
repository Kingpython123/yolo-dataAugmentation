"""一次性迁移脚本: 把 src/generate.py 接入 reporting 边界 + 结构化事件 + 取消支持。

为什么用脚本而不是编辑器改:
  本机的 str_replace 工具会把 UTF-8 中文源文件按 GBK 往返一次, 导致整文件乱码。
  这里显式用 encoding="utf-8" 读写, 并保留原有的 CRLF 行尾。

策略: 全有或全无。任何一个待替换片段没有精确命中一次就整体放弃, 不留半改的文件。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src" / "generate.py"

# (说明, 原文, 新文) —— 原文必须在文件中精确出现且仅出现一次
EDITS: list[tuple[str, str, str]] = [
    (
        "导入: 去掉 tqdm, 引入 reporting",
        'import numpy as np\n'
        'from PIL import Image\n'
        'from tqdm import tqdm\n'
        '\n'
        'from . import mask_utils, quality_check, structure_ref\n',

        'import numpy as np\n'
        'from PIL import Image\n'
        '\n'
        'from . import mask_utils, quality_check, reporting, structure_ref\n',
    ),
    (
        "新增取消标记常量",
        '_safe = safe_name  # 目录/文件名净化(与 requalify 共用同一实现)\n',

        '_safe = safe_name  # 目录/文件名净化(与 requalify 共用同一实现)\n'
        '\n'
        '# 取消标记。用一个不可能与真实错误信息重合的哨兵值, 让主循环把"用户主动\n'
        '# 停止"与"任务失败"区分开: 前者不写 fail_log, 也不计入失败率统计, 否则\n'
        '# 一次手动停止会在失败日志里留下几百条噪声, 把真正的失败原因埋掉。\n'
        'CANCELLED = "__cancelled__"\n',
    ),
    (
        "usable_references 的过滤提示",
        '        detail = ", ".join(f"{k}≥{v}" for k, v in by_type.items())\n'
        '        print(f"[filter] 参考缺陷按 severity≥{base}"\n'
        '              f"{f\' ({detail})\' if detail else \'\'} 过滤: "\n'
        '              f"{len(records)} -> {len(kept)} 条")\n',

        '        detail = ", ".join(f"{k}≥{v}" for k, v in by_type.items())\n'
        '        reporting.info(f"[filter] 参考缺陷按 severity≥{base}"\n'
        '                       f"{f\' ({detail})\' if detail else \'\'} 过滤: "\n'
        '                       f"{len(records)} -> {len(kept)} 条")\n',
    ),
    (
        "结构图生成失败告警",
        '                print(f"[warn] 结构图生成失败, 退回仅用彩色参考: {e}")\n',
        '                reporting.warn(f"[warn] 结构图生成失败, 退回仅用彩色参考: {e}")\n',
    ),
    (
        "编辑调用失败告警",
        '            print(f"[warn] 编辑失败(尝试{attempt+1}, {reason}): {e}")\n',
        '            reporting.warn(f"[warn] 编辑失败(尝试{attempt+1}, {reason}): {e}")\n',
    ),
    (
        "掩膜为空但改动很大的提示",
        '                print(f"[info] 掩膜为空但模型改动很大(超阈值像素"\n'
        '                      f"{mask_info[\'over_thresh_ratio\']:.2f}), 下轮要求收敛改动范围")\n',

        '                reporting.info(f"[info] 掩膜为空但模型改动很大(超阈值像素"\n'
        '                               f"{mask_info[\'over_thresh_ratio\']:.2f}), "\n'
        '                               f"下轮要求收敛改动范围")\n',
    ),
    (
        "_run_tasks 签名: 增加 cancel 参数",
        'def _run_tasks(cfg: Config, tasks: list[Task], resume: bool = True):\n'
        '    """统一的并发执行器, generate 与 gen-ref 共用(问题10: 消除重复逻辑)。"""\n',

        'def _run_tasks(cfg: Config, tasks: list[Task], resume: bool = True,\n'
        '               cancel: threading.Event | None = None):\n'
        '    """统一的并发执行器, generate 与 gen-ref 共用(问题10: 消除重复逻辑)。\n'
        '\n'
        '    cancel: 置位后不再启动新任务, 已在途的任务跑完并正常落盘。刻意不去强杀\n'
        '    线程, 因为 annotations.jsonl 是追加写的, 中途打断会留下半行 JSON, 下次\n'
        '    断点续跑解析到那一行就会丢掉整批已完成记录。\n'
        '    """\n',
    ),
    (
        "计划阶段: 结构化事件 + 输出边界",
        '    if skipped:\n'
        '        print(f"[resume] 跳过已完成 {skipped} 项, 待生成 {len(pending)} 项")\n'
        '    if not pending:\n'
        '        print("[done] 没有待生成的任务")\n'
        '        return\n',

        '    reporting.event("plan", total=len(tasks), skipped=skipped,\n'
        '                    pending=len(pending))\n'
        '    if skipped:\n'
        '        reporting.info(f"[resume] 跳过已完成 {skipped} 项, 待生成 {len(pending)} 项")\n'
        '    if not pending:\n'
        '        reporting.info("[done] 没有待生成的任务")\n'
        '        reporting.event("finished", status="nothing_to_do", stats={\n'
        '            "ok": 0, "best_effort": 0, "rejected": 0,\n'
        '            "failed": 0, "cancelled": 0})\n'
        '        return\n',
    ),
    (
        "stats 增加 cancelled 计数",
        '    stats = {"ok": 0, "best_effort": 0, "rejected": 0, "failed": 0}\n',
        '    stats = {"ok": 0, "best_effort": 0, "rejected": 0, "failed": 0,\n'
        '             "cancelled": 0}\n',
    ),
    (
        "work(): 入口检查取消",
        '    def work(t: Task):\n'
        '        seed = gen.get("seed", 42)\n',

        '    def work(t: Task):\n'
        '        # 尚未启动的任务在这里直接退出, 不发起任何 API 调用(不烧额度)\n'
        '        if cancel is not None and cancel.is_set():\n'
        '            return t, None, CANCELLED\n'
        '        seed = gen.get("seed", 42)\n',
    ),
    (
        "主循环: tqdm -> reporting.track",
        '        for fut in tqdm(as_completed(futs), total=len(futs), desc="生成"):\n'
        '            t, res, err = fut.result()\n'
        '            if err or res is None:\n',

        '        done_count = 0\n'
        '        for fut in reporting.track(as_completed(futs), total=len(futs),\n'
        '                                   desc="生成"):\n'
        '            t, res, err = fut.result()\n'
        '            if err == CANCELLED:\n'
        '                stats["cancelled"] += 1\n'
        '                continue\n'
        '            done_count += 1\n'
        '            reporting.progress_tick(done_count, len(futs))\n'
        '            if err or res is None:\n',
    ),
    (
        "失败分支: 输出边界",
        '                    print(f"[warn] {t.clean_path.name} ({reason}): {err}")\n',
        '                    reporting.warn(f"[warn] {t.clean_path.name} ({reason}): {err}")\n',
    ),
    (
        "落盘后发出单任务事件",
        '            with ann_lock:\n'
        '                ann_f.write(json.dumps(meta, ensure_ascii=False) + "\\n")\n'
        '                ann_f.flush()\n',

        '            with ann_lock:\n'
        '                ann_f.write(json.dumps(meta, ensure_ascii=False) + "\\n")\n'
        '                ann_f.flush()\n'
        '            reporting.event(\n'
        '                "task", stem=t.stem, cls=t.class_name,\n'
        '                verdict=("rejected" if rejected\n'
        '                         else meta.get("accepted_as") or "ok"),\n'
        '                realism=meta.get("qc", {}).get("realism"),\n'
        '                attempt=meta.get("attempt"))\n',
    ),
    (
        "收尾统计: 输出边界 + finished 事件",
        '    ann_f.close()\n'
        '    print(f"\\n[done] 合格={stats[\'ok\']} 兜底={stats[\'best_effort\']} "\n'
        '          f"驳回={stats[\'rejected\']} 失败={stats[\'failed\']}")\n'
        '    print(f"       标注: {ann_path}")\n'
        '    if stats["rejected"]:\n'
        '        print(f"       驳回样本另存于: {rej_dir} (不混入训练集)")\n'
        '    if stats["failed"]:\n'
        '        fail_log = cfg.resolve(cfg.output.get("root", "outputs")) / "fail_log.jsonl"\n'
        '        print(f"       失败原因日志: {fail_log}")\n'
        '        print(f"       (用 `run.py fail-summary` 查看本次失败原因分类统计)")\n',

        '    ann_f.close()\n'
        '    reporting.info(f"\\n[done] 合格={stats[\'ok\']} 兜底={stats[\'best_effort\']} "\n'
        '                   f"驳回={stats[\'rejected\']} 失败={stats[\'failed\']}")\n'
        '    reporting.info(f"       标注: {ann_path}")\n'
        '    if stats["cancelled"]:\n'
        '        reporting.info(f"       已取消未开始的任务: {stats[\'cancelled\']} 项"\n'
        '                       f"(可重跑同样命令续跑)")\n'
        '    if stats["rejected"]:\n'
        '        reporting.info(f"       驳回样本另存于: {rej_dir} (不混入训练集)")\n'
        '    if stats["failed"]:\n'
        '        fail_log = cfg.resolve(cfg.output.get("root", "outputs")) / "fail_log.jsonl"\n'
        '        reporting.info(f"       失败原因日志: {fail_log}")\n'
        '        reporting.info(f"       (用 `run.py fail-summary` 查看本次失败原因分类统计)")\n'
        '    reporting.event(\n'
        '        "finished",\n'
        '        status=("cancelled" if cancel is not None and cancel.is_set()\n'
        '                else "completed"),\n'
        '        stats=dict(stats))\n',
    ),
    (
        "_warn_legacy 输出边界",
        '    if legacy:\n'
        '        print(f"[warn] 缺陷库中有 {len(legacy)}/{len(records)} 条为旧格式"\n'
        '              f"(缺 severity 等细化字段), 建议 build-catalog --overwrite 重建")\n',

        '    if legacy:\n'
        '        reporting.warn(\n'
        '            f"[warn] 缺陷库中有 {len(legacy)}/{len(records)} 条为旧格式"\n'
        '            f"(缺 severity 等细化字段), 建议 build-catalog --overwrite 重建")\n',
    ),
    # ---------------- 对外入口: 透传 cancel ----------------
    (
        "generate(): 透传 cancel",
        'def generate(cfg: Config, limit_per_class: int | None = None,\n'
        '             only_classes: list[str] | None = None, resume: bool = True):\n',

        'def generate(cfg: Config, limit_per_class: int | None = None,\n'
        '             only_classes: list[str] | None = None, resume: bool = True,\n'
        '             cancel: threading.Event | None = None):\n',
    ),
    (
        "generate(): 计划输出与调用",
        '    print(f"[plan] 共 {len(tasks)} 个生成任务, 覆盖 {len(clean_images)} 个类别")\n'
        '    _run_tasks(cfg, tasks, resume=resume)\n',

        '    reporting.info(f"[plan] 共 {len(tasks)} 个生成任务, 覆盖 {len(clean_images)} 个类别")\n'
        '    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)\n',
    ),
    (
        "generate_sweep(): 签名",
        'def generate_sweep(cfg: Config, classes: list[str], resume: bool = True,\n'
        '                   shuffle: bool = False, max_refs: int | None = None):\n',

        'def generate_sweep(cfg: Config, classes: list[str], resume: bool = True,\n'
        '                   shuffle: bool = False, max_refs: int | None = None,\n'
        '                   cancel: threading.Event | None = None):\n',
    ),
    (
        "generate_sweep(): 无干净图告警",
        '        paths = clean_images.get(class_name)\n'
        '        if not paths:\n'
        '            print(f"[warn] 类别无干净图, 跳过: {class_name}")\n'
        '            continue\n'
        '        take = min(len(paths), len(records) - cursor)\n',

        '        paths = clean_images.get(class_name)\n'
        '        if not paths:\n'
        '            reporting.warn(f"[warn] 类别无干净图, 跳过: {class_name}")\n'
        '            continue\n'
        '        take = min(len(paths), len(records) - cursor)\n',
    ),
    (
        "generate_sweep(): 计划输出与调用",
        '    print(f"[plan] 可用参考缺陷 {len(records)} 条, "\n'
        '          f"每条只用一次, 共 {len(tasks)} 个任务")\n'
        '    for class_name, take in plan:\n'
        '        print(f"        {class_name}: {take} 张")\n'
        '    left = len(records) - cursor\n'
        '    if left > 0:\n'
        '        print(f"[warn] 还有 {left} 条参考没分配到干净图, "\n'
        '              f"请在 --classes 后面追加更多类别")\n'
        '    _run_tasks(cfg, tasks, resume=resume)\n',

        '    reporting.info(f"[plan] 可用参考缺陷 {len(records)} 条, "\n'
        '                   f"每条只用一次, 共 {len(tasks)} 个任务")\n'
        '    for class_name, take in plan:\n'
        '        reporting.info(f"        {class_name}: {take} 张")\n'
        '    left = len(records) - cursor\n'
        '    if left > 0:\n'
        '        reporting.warn(f"[warn] 还有 {left} 条参考没分配到干净图, "\n'
        '                       f"请在 --classes 后面追加更多类别")\n'
        '    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)\n',
    ),
    (
        "generate_target(): 签名",
        'def generate_target(cfg: Config, classes: list[str], count: int,\n'
        '                    resume: bool = True) -> None:\n',

        'def generate_target(cfg: Config, classes: list[str], count: int,\n'
        '                    resume: bool = True,\n'
        '                    cancel: threading.Event | None = None) -> None:\n',
    ),
    (
        "generate_target(): 计划输出",
        '        paths = clean_images.get(class_name)\n'
        '        if not paths:\n'
        '            print(f"[warn] 类别无干净图, 跳过: {class_name}")\n'
        '            continue\n'
        '        n_clean = len(paths)\n',

        '        paths = clean_images.get(class_name)\n'
        '        if not paths:\n'
        '            reporting.warn(f"[warn] 类别无干净图, 跳过: {class_name}")\n'
        '            continue\n'
        '        n_clean = len(paths)\n',
    ),
    (
        "generate_target(): 覆盖计划输出",
        '        print(f"[plan] {class_name}: 目标 {count} 张, 参考库 {n_ref} 条 "\n'
        '              f"({cover_msg}), 干净图 {n_clean} 张(循环使用)")\n',

        '        reporting.info(f"[plan] {class_name}: 目标 {count} 张, 参考库 {n_ref} 条 "\n'
        '                       f"({cover_msg}), 干净图 {n_clean} 张(循环使用)")\n',
    ),
    (
        "generate_target(): 汇总与调用",
        '    print(f"\\n[plan] 共 {len(tasks)} 个任务")\n'
        '    _run_tasks(cfg, tasks, resume=resume)\n',

        '    reporting.info(f"\\n[plan] 共 {len(tasks)} 个任务")\n'
        '    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)\n',
    ),
    (
        "generate_with_reference(): 签名",
        'def generate_with_reference(cfg: Config, reference_entry: str,\n'
        '                            classes: list[str], per_class: int = 1,\n'
        '                            resume: bool = True):\n',

        'def generate_with_reference(cfg: Config, reference_entry: str,\n'
        '                            classes: list[str], per_class: int = 1,\n'
        '                            resume: bool = True,\n'
        '                            cancel: threading.Event | None = None):\n',
    ),
    (
        "generate_with_reference(): 参考信息输出",
        '    print(f"[参考缺陷] {ref[\'entry_id\']} | 类型={ref[\'defect_type\']} "\n'
        '          f"| severity={ref.get(\'severity\',\'?\')} | {ref.get(\'appearance\',\'\')}")\n',

        '    reporting.info(f"[参考缺陷] {ref[\'entry_id\']} | 类型={ref[\'defect_type\']} "\n'
        '                   f"| severity={ref.get(\'severity\',\'?\')} | "\n'
        '                   f"{ref.get(\'appearance\',\'\')}")\n',
    ),
    (
        "generate_with_reference(): 收尾",
        '        paths = clean_images.get(class_name)\n'
        '        if not paths:\n'
        '            print(f"[warn] 类别无干净图: {class_name}")\n'
        '            continue\n',

        '        paths = clean_images.get(class_name)\n'
        '        if not paths:\n'
        '            reporting.warn(f"[warn] 类别无干净图: {class_name}")\n'
        '            continue\n',
    ),
    (
        "generate_with_reference(): 计划输出与调用",
        '    print(f"[plan] 共 {len(tasks)} 个定向生成任务")\n'
        '    _run_tasks(cfg, tasks, resume=resume)\n',

        '    reporting.info(f"[plan] 共 {len(tasks)} 个定向生成任务")\n'
        '    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)\n',
    ),
]


def main() -> int:
    with TARGET.open("r", encoding="utf-8", newline="") as fh:
        raw = fh.read()
    uses_crlf = "\r\n" in raw
    text = raw.replace("\r\n", "\n")

    problems: list[str] = []
    for label, old, _new in EDITS:
        n = text.count(old)
        if n != 1:
            problems.append(f"  [{label}] 命中 {n} 次(期望 1)")
    if problems:
        print("以下片段未精确命中, 未做任何修改:", file=sys.stderr)
        print("\n".join(problems), file=sys.stderr)
        return 1

    for label, old, new in EDITS:
        text = text.replace(old, new, 1)
        print(f"  ok  {label}")

    if uses_crlf:
        text = text.replace("\n", "\r\n")
    with TARGET.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    print(f"\n已写回 {TARGET} (共 {len(EDITS)} 处, 行尾={'CRLF' if uses_crlf else 'LF'})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
