"""验证 gen-augment 的方式A(属性筛)与方式C(按产出图反查)。

全部走真实 CLI 入口, 不加 --yes 因此绝不会调用 API。捕获 stdout 落盘查看,
避免 PowerShell 控制台 GBK 显示导致中文乱码。

验证点:
  1. 方式A 的属性筛选命中数与手工统计一致(用严重度4-5、横向 等条件交叉验证)
  2. 方式C 从"没打标签的"30 张反查出的参考条目数正确(应为去重后的条目数)
  3. 两种方式组合时取并集且不重复
  4. 缺少任何挑选方式时给出可操作的提示并返回非 0
  5. 预览模式不产生任何副作用
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

REJECTED_DIR = "没打标签的"


def run_cli(argv: list[str]) -> tuple[int, str]:
    from run import main as run_main
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["run.py", *argv]
    try:
        with contextlib.redirect_stdout(buf):
            code = run_main()
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    finally:
        sys.argv = old_argv
    return code, buf.getvalue()


def main() -> int:
    from src.config import load_config
    from src.dataset import safe_name
    from src.rejection import extract_reference_ids

    cfg = load_config()
    catalog_path = cfg.out_path("catalog") / "catalog.json"
    records = json.loads(catalog_path.read_text(encoding="utf-8"))

    out: list[str] = []
    problems: list[str] = []

    # ---------- 独立算出期望值, 用于交叉验证 CLI 的输出 ----------
    expect_sev45 = sum(1 for r in records
                       if 4 <= int(r.get("severity", 3) or 3) <= 5)
    expect_sev45_heng = sum(
        1 for r in records
        if 4 <= int(r.get("severity", 3) or 3) <= 5
        and "横向" in str(r.get("orientation", "") or ""))

    rejected_dir = cfg.resolve(REJECTED_DIR)
    rep_c = extract_reference_ids([rejected_dir], records)
    expect_from_images = len(rep_c.entry_ids)

    out.append("=== 独立统计的期望值 ===")
    out.append(f"缺陷库总条目: {len(records)}")
    out.append(f"严重度 4-5: {expect_sev45}")
    out.append(f"严重度 4-5 且走向含'横向': {expect_sev45_heng}")
    out.append(f"从 {REJECTED_DIR} 反查出的去重参考条目: {expect_from_images}")
    out.append(f"  (该目录 {rep_c.scanned_files} 个文件, "
               f"反查失败 {len(rep_c.problems)})")
    out.append("")

    # ---------- 用例1: 方式A 严重度筛选 ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--severity", "4-5"])
    out.append("=== 用例1: 方式A --severity 4-5 ===")
    out.append(f"exit={code}")
    out.append(text)
    if code != 0:
        problems.append(f"用例1 期望 exit=0, 实际 {code}")
    if f"{expect_sev45}/{len(records)} 条命中" not in text:
        problems.append(f"用例1 命中数应为 {expect_sev45}/{len(records)}")
    if "未调用任何 API" not in text:
        problems.append("用例1 预览模式应提示未调用 API")

    # ---------- 用例2: 方式A 严重度 + 走向 组合 ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--severity", "4-5", "--orientation", "横向"])
    out.append("=== 用例2: 方式A --severity 4-5 --orientation 横向 ===")
    out.append(f"exit={code}")
    out.append(text)
    if f"{expect_sev45_heng}/{len(records)} 条命中" not in text:
        problems.append(f"用例2 命中数应为 {expect_sev45_heng}/{len(records)}")

    # ---------- 用例3: 方式C 从产出图反查 ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--from-images", REJECTED_DIR])
    out.append("=== 用例3: 方式C --from-images 没打标签的 ===")
    out.append(f"exit={code}")
    out.append(text)
    if code != 0:
        problems.append(f"用例3 期望 exit=0, 实际 {code}")
    if f"反查出参考条目 {expect_from_images} 条" not in text:
        problems.append(f"用例3 反查条目数应为 {expect_from_images}")
    if f"最终选中参考条目 {expect_from_images} 条" not in text:
        problems.append("用例3 最终选中数应等于反查出的条目数")

    # ---------- 用例4: 方式A + 方式C 组合(取并集) ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--from-images", REJECTED_DIR,
                         "--severity", "5"])
    out.append("=== 用例4: 方式C + 方式A(severity=5) 组合 ===")
    out.append(f"exit={code}")
    out.append(text)
    expect_sev5 = sum(1 for r in records
                      if int(r.get("severity", 3) or 3) == 5)
    ids_c = set(rep_c.entry_ids)
    ids_a = {r["entry_id"] for r in records
             if int(r.get("severity", 3) or 3) == 5}
    expect_union = len(ids_c | ids_a)
    out.append(f"(期望并集: |C|={len(ids_c)} |A|={expect_sev5} "
               f"并集={expect_union})")
    if f"最终选中参考条目 {expect_union} 条" not in text:
        problems.append(f"用例4 并集应为 {expect_union} 条")

    # ---------- 用例5: 什么都不给, 应报错并给出提示 ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg"])
    out.append("=== 用例5: 未给任何挑选方式 ===")
    out.append(f"exit={code}")
    out.append(text)
    if code == 0:
        problems.append("用例5 应返回非 0")
    if "--from-images" not in text:
        problems.append("用例5 应提示可用的挑选方式")

    # ---------- 用例6: --max-refs 截断 ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--severity", "4-5", "--max-refs", "3",
                         "--per-ref", "2"])
    out.append("=== 用例6: --max-refs 3 --per-ref 2 ===")
    out.append(f"exit={code}")
    out.append(text)
    if "最终选中参考条目 3 条" not in text:
        problems.append("用例6 应截断为 3 条")
    if "将生成 6 张样本" not in text:
        problems.append("用例6 应生成 3 参考 x 1 类别 x 2 张 = 6 张")

    # ---------- 副作用检查 ----------
    out.append("=== 副作用检查 ===")
    side = []
    gen_dir = cfg.out_path("images")
    if gen_dir.exists():
        n = len(list(gen_dir.rglob("*aug*.png")))
        side.append(f"outputs/generated 下含 aug 的文件数: {n}")
        if n:
            problems.append(f"预览模式不应产生任何 aug 产物, 实际 {n} 个")
    else:
        side.append("outputs/generated 不存在(符合预期)")
    out.extend(side)

    out.append("")
    out.append("PASS" if not problems else "FAIL:\n" + "\n".join(problems))
    (REPO / "tmp_augment_cli_result.txt").write_text(
        "\n".join(out) + "\n", encoding="utf-8")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
