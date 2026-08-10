"""验证 plan_regenerate_rejected() 对真实"没打标签的"30 张驳回样本的规划结果。

只调用规划函数, 不触发任何网络请求, 用于在接 CLI 子命令前先确认核心逻辑正确:
  1. 反查数量与 tools/probe_rejected.py 之前验证过的 30/30 一致
  2. 每个任务的 salt 与 stem 一致(裁块位置会因 __r2 后缀而与原图不同)
  3. 断点续跑语义: 已存在 outputs/regenerated/<类别>/<stem>.png 的会被跳过
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from src.config import load_config  # noqa: E402
from src.generate import plan_regenerate_rejected  # noqa: E402

REJECTED_DIR = REPO / "没打标签的"


def main() -> int:
    cfg = load_config()
    tasks, scan_report, skipped = plan_regenerate_rejected(cfg, [REJECTED_DIR])

    lines = [
        f"扫描目录: {scan_report.scanned_dirs}",
        f"扫描文件数: {scan_report.scanned_files}",
        f"反查成功: {scan_report.ok_count}",
        f"反查失败: {scan_report.problem_count}",
        f"已重生成过(跳过): {len(skipped)}",
        f"本次待重生成: {len(tasks)}",
        "",
    ]
    for p in scan_report.problems[:10]:
        lines.append(f"  problem: {p}")

    lines.append("")
    for t in tasks[:8]:
        lines.append(f"  stem={t.stem}")
        lines.append(f"    类别={t.class_name}  干净图={t.clean_path.name}  "
                     f"index={t.index}")
        lines.append(f"    salt={t.salt}  参考={t.forced_ref['entry_id']}")

    problems: list[str] = []
    if scan_report.ok_count != 30:
        problems.append(f"期望反查成功 30, 实际 {scan_report.ok_count}")
    if scan_report.problem_count != 0:
        problems.append(f"期望反查失败 0, 实际 {scan_report.problem_count}")
    if len(tasks) + len(skipped) != scan_report.ok_count:
        problems.append("待重生成 + 已跳过 应等于 反查成功总数, 但不等")

    # 每个任务的 stem 必须以 __r 数字结尾, 且与 salt 一致(用于验证位置重挑机制接线正确)
    import re
    round_re = re.compile(r"__r\d+$")
    for t in tasks:
        if not round_re.search(t.stem):
            problems.append(f"stem 缺少轮次后缀: {t.stem}")
        if t.salt != t.stem:
            problems.append(f"salt 与 stem 不一致: salt={t.salt} stem={t.stem}")

    (REPO / "tmp_regen_plan_result.txt").write_text(
        "\n".join(lines) + "\n\n"
        + ("PASS\n" if not problems else "FAIL:\n" + "\n".join(problems) + "\n"),
        encoding="utf-8")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
