"""一次性迁移: 给 src/generate.py 加 salt 机制 + regenerate_rejected() 规划器。

策略同 tools/migrate_generate.py: 全有或全无, 显式 utf-8 读写并保留 CRLF,
避免本机 str_replace 工具把中文源文件按 GBK 往返导致乱码。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "src" / "generate.py"

EDITS: list[tuple[str, str, str]] = [
    (
        "Task 增加 salt 字段",
        "@dataclass\n"
        "class Task:\n"
        "    class_name: str\n"
        "    clean_path: Path\n"
        "    index: int\n"
        "    forced_ref: dict | None = None\n"
        "    stem: str = \"\"\n",

        "@dataclass\n"
        "class Task:\n"
        "    class_name: str\n"
        "    clean_path: Path\n"
        "    index: int\n"
        "    forced_ref: dict | None = None\n"
        "    stem: str = \"\"\n"
        "    # 参与 RNG 播种但不影响文件命名。用于\"同一张干净图 + 同一条参考\"\n"
        "    # 需要重新挑一次裁块位置的场景(人工驳回样本重生成、定向补充素材)。\n"
        "    # 为空时播种字符串与改造前逐字相同, 不影响任何既有任务的可复现性。\n"
        "    salt: str = \"\"\n",
    ),
    (
        "work(): 播种字符串纳入 salt",
        "        seed = gen.get(\"seed\", 42)\n"
        "        rng = random.Random(f\"{seed}|{t.class_name}|{t.clean_path.name}|{t.index}\")\n",

        "        seed = gen.get(\"seed\", 42)\n"
        "        seed_str = f\"{seed}|{t.class_name}|{t.clean_path.name}|{t.index}\"\n"
        "        if t.salt:\n"
        "            seed_str += f\"|{t.salt}\"\n"
        "        rng = random.Random(seed_str)\n",
    ),
    (
        "_run_tasks 签名: 增加可选的产物目录覆盖",
        "def _run_tasks(cfg: Config, tasks: list[Task], resume: bool = True,\n"
        "               cancel: threading.Event | None = None):\n"
        "    \"\"\"统一的并发执行器, generate 与 gen-ref 共用(问题10: 消除重复逻辑)。\n"
        "\n"
        "    cancel: 置位后不再启动新任务, 已在途的任务跑完并正常落盘。刻意不去强杀\n"
        "    线程, 因为 annotations.jsonl 是追加写的, 中途打断会留下半行 JSON, 下次\n"
        "    断点续跑解析到那一行就会丢掉整批已完成记录。\n"
        "    \"\"\"\n",

        "def _run_tasks(cfg: Config, tasks: list[Task], resume: bool = True,\n"
        "               cancel: threading.Event | None = None,\n"
        "               img_dir_override: Path | None = None,\n"
        "               rej_dir_override: Path | None = None,\n"
        "               ann_path_override: Path | None = None):\n"
        "    \"\"\"统一的并发执行器, generate 与 gen-ref 共用(问题10: 消除重复逻辑)。\n"
        "\n"
        "    cancel: 置位后不再启动新任务, 已在途的任务跑完并正常落盘。刻意不去强杀\n"
        "    线程, 因为 annotations.jsonl 是追加写的, 中途打断会留下半行 JSON, 下次\n"
        "    断点续跑解析到那一行就会丢掉整批已完成记录。\n"
        "\n"
        "    *_override: 让\"驳回样本重生成\"\"定向补充\"这类规划器落盘到不同目录\n"
        "    (outputs/regenerated/ 等), 不与主批次的产物、标注混在一起。不传时\n"
        "    行为与改造前完全一致, 因此主批次生成不受影响。\n"
        "    \"\"\"\n",
    ),
    (
        "_run_tasks 内部使用覆盖目录",
        "    gen = cfg.generation\n"
        "    img_dir = cfg.out_path(\"images\")\n"
        "    mask_dir = cfg.out_path(\"masks\")\n"
        "    rej_dir = cfg.resolve(cfg.output.get(\"rejected\", \"outputs/rejected\"))\n"
        "    ann_path = cfg.out_path(\"annotations\")\n",

        "    gen = cfg.generation\n"
        "    img_dir = img_dir_override or cfg.out_path(\"images\")\n"
        "    mask_dir = cfg.out_path(\"masks\")\n"
        "    rej_dir = rej_dir_override or cfg.resolve(\n"
        "        cfg.output.get(\"rejected\", \"outputs/rejected\"))\n"
        "    ann_path = ann_path_override or cfg.out_path(\"annotations\")\n"
        "    img_dir.mkdir(parents=True, exist_ok=True)\n"
        "    ann_path.parent.mkdir(parents=True, exist_ok=True)\n",
    ),
    (
        "generate_with_reference() 之后追加新规划器",
        "        for p in rng.sample(paths, min(per_class, len(paths))):\n"
        "            tasks.append(Task(class_name, p, 0, forced_ref=ref,\n"
        "                              stem=f\"{_safe(p.stem)}__ref_{_safe(ref['entry_id'])}\"))\n"
        "    reporting.info(f\"[plan] 共 {len(tasks)} 个定向生成任务\")\n"
        "    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)\n",

        "        for p in rng.sample(paths, min(per_class, len(paths))):\n"
        "            tasks.append(Task(class_name, p, 0, forced_ref=ref,\n"
        "                              stem=f\"{_safe(p.stem)}__ref_{_safe(ref['entry_id'])}\"))\n"
        "    reporting.info(f\"[plan] 共 {len(tasks)} 个定向生成任务\")\n"
        "    _run_tasks(cfg, tasks, resume=resume, cancel=cancel)\n"
        "\n"
        "\n"
        "# --------------------------- 驳回样本重生成 ---------------------------\n"
        "\n"
        "def _next_round_suffix(out_dir: Path, original_stem: str) -> str:\n"
        "    \"\"\"扫已存在的 {original_stem}__r*.png, 返回下一个可用轮次后缀。\n"
        "\n"
        "    从 __r2 开始(__r1 省略不用, 避免与\"第一次生成\"混淆); 找不到已有轮次时\n"
        "    直接给 __r2。轮次号必须探测而不能固定, 否则同一张驳回图第二次重跑会\n"
        "    覆盖第一次重生成的结果, 人工没法对比多轮效果。\n"
        "    \"\"\"\n"
        "    existing = set()\n"
        "    if out_dir.exists():\n"
        "        prefix = f\"{original_stem}__r\"\n"
        "        for p in out_dir.glob(f\"{prefix}*.png\"):\n"
        "            tail = p.stem[len(prefix):]\n"
        "            if tail.isdigit():\n"
        "                existing.add(int(tail))\n"
        "    n = 2\n"
        "    while n in existing:\n"
        "        n += 1\n"
        "    return f\"__r{n}\"\n"
        "\n"
        "\n"
        "def plan_regenerate_rejected(\n"
        "        cfg: Config, directories: list, reroll_ref: bool = False,\n"
        "        catalog_records: list[dict] | None = None):\n"
        "    \"\"\"从人工驳回样本目录反查上下文, 规划出重生成任务列表。\n"
        "\n"
        "    只负责\"规划\", 不落盘不调用 API —— dry-run 与真跑共用这一份规划结果,\n"
        "    保证 --dry-run 打印的清单与真正执行的任务完全一致。\n"
        "\n"
        "    返回 (tasks, scan_report, skipped): skipped 是反查成功但目标产物已存在\n"
        "    的任务(视为已经重生成过, 断点续跑语义与其它规划器一致)。\n"
        "    \"\"\"\n"
        "    from . import rejection\n"
        "\n"
        "    records = catalog_records if catalog_records is not None else load_catalog(cfg)\n"
        "    if not records:\n"
        "        raise RuntimeError(\"缺陷库为空, 请先成功运行 build-catalog\")\n"
        "    by_class: dict[str, list[dict]] = defaultdict(list)\n"
        "    for r in records:\n"
        "        by_class[r[\"class_name\"]].append(r)\n"
        "\n"
        "    scan_report = rejection.scan_rejected_dirs(cfg, directories, records)\n"
        "\n"
        "    regen_root = cfg.resolve(cfg.output.get(\"regenerated\", \"outputs/regenerated\"))\n"
        "    rng = random.Random(cfg.generation.get(\"seed\", 42))\n"
        "\n"
        "    tasks: list[Task] = []\n"
        "    skipped: list = []\n"
        "    for item in scan_report.resolved:\n"
        "        ref = item.ref_record\n"
        "        if reroll_ref:\n"
        "            pool = by_class.get(item.class_name) or records\n"
        "            # 排除掉原参考, 否则\"换一条\"有较大概率抽回同一条(概率随库内\n"
        "            # 同类别条目数减少而升高), 与用户意图(换个思路重试)相悖\n"
        "            alt_pool = [r for r in pool if r[\"entry_id\"] != item.ref_entry_id]\n"
        "            ref = rng.choice(alt_pool or pool)\n"
        "\n"
        "        out_dir = regen_root / _safe(item.class_name)\n"
        "        suffix = _next_round_suffix(out_dir, item.original_stem)\n"
        "        stem = f\"{item.original_stem}{suffix}\"\n"
        "\n"
        "        if (out_dir / f\"{stem}.png\").exists():\n"
        "            skipped.append(item)\n"
        "            continue\n"
        "\n"
        "        # salt 用重生成的目标 stem 本身: 同一张驳回图多轮重生成时,\n"
        "        # stem 因轮次号不同而不同, 天然保证每轮换到不同的裁块位置。\n"
        "        tasks.append(Task(\n"
        "            item.class_name, item.clean_path, item.index,\n"
        "            forced_ref=ref, stem=stem, salt=stem))\n"
        "    return tasks, scan_report, skipped\n"
        "\n"
        "\n"
        "def regenerate_rejected(\n"
        "        cfg: Config, directories: list, reroll_ref: bool = False,\n"
        "        cancel: threading.Event | None = None) -> dict:\n"
        "    \"\"\"重新生成人工驳回样本, 落盘到 outputs/regenerated/ 供人工二次 review。\n"
        "\n"
        "    刻意不写进 outputs/generated/: 这批本来就是被挑出来的不合格样本,\n"
        "    重新生成后仍应该先让人看一眼, 由用户自行决定要不要合并进训练集\n"
        "    (需求明确不要程序代管合并)。\n"
        "\n"
        "    产物目录与标注文件都换成 regenerated 下的独立位置(经 _run_tasks 的\n"
        "    *_override 参数), 不与主批次的 outputs/generated、\n"
        "    outputs/annotations.jsonl 混在一起, 也就不会干扰主批次的断点续跑。\n"
        "    \"\"\"\n"
        "    tasks, scan_report, skipped = plan_regenerate_rejected(\n"
        "        cfg, directories, reroll_ref=reroll_ref)\n"
        "\n"
        "    if scan_report.problems:\n"
        "        for p in scan_report.problems:\n"
        "            reporting.warn(f\"[warn] {p}\")\n"
        "    reporting.info(\n"
        "        f\"[plan] 扫描 {scan_report.scanned_files} 个文件, \"\n"
        "        f\"反查成功 {scan_report.ok_count}, 反查失败 {scan_report.problem_count}, \"\n"
        "        f\"已重生成过跳过 {len(skipped)}, 待重生成 {len(tasks)}\")\n"
        "\n"
        "    if not tasks:\n"
        "        reporting.info(\"[done] 没有待重生成的任务\")\n"
        "        return {\"planned\": 0, \"skipped\": len(skipped),\n"
        "                \"problems\": len(scan_report.problems)}\n"
        "\n"
        "    regen_root = cfg.resolve(cfg.output.get(\"regenerated\", \"outputs/regenerated\"))\n"
        "    # 重生成过程中若再次判定不合格, 也落在 regenerated 下便于对比,\n"
        "    # 不能混进主批次的 outputs/rejected/\n"
        "    _run_tasks(cfg, tasks, resume=False, cancel=cancel,\n"
        "              img_dir_override=regen_root,\n"
        "              rej_dir_override=regen_root / \"_rejected\",\n"
        "              ann_path_override=regen_root / \"annotations.jsonl\")\n"
        "\n"
        "    return {\"planned\": len(tasks), \"skipped\": len(skipped),\n"
        "            \"problems\": len(scan_report.problems)}\n",
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
    print(f"\n已写回 {TARGET} (共 {len(EDITS)} 处)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
