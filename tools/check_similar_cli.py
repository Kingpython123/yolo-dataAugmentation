"""验证 find-similar 子命令与 gen-augment 的方式 B。

全部走真实 CLI 入口, gen-augment 不加 --yes 因此绝不调用 API。

验证点:
  1. find-similar --entry 能返回结果, 且不含种子自身
  2. find-similar --image 用缺陷库自己的裁剪图当查询, 第 1 名应是该条目本身
  3. find-similar 缺少种子时 exit=2 且给出两种种子的提示
  4. gen-augment --similar-to-entry 的方式 B 正常挑选并进入预览
  5. gen-augment 方式 B + 方式 A 组合取并集
  6. gen-augment 无任何挑选方式时的提示已包含方式 B
  7. 预览模式零副作用
"""
from __future__ import annotations

import contextlib
import io
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


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

    cfg = load_config()
    records = json.loads(
        (cfg.out_path("catalog") / "catalog.json").read_text(encoding="utf-8"))
    by_id = {r["entry_id"]: r for r in records}

    out: list[str] = []
    problems: list[str] = []

    # 选一条有裁剪图的变形条目作为种子
    seed = next(r for r in records
                if r.get("defect_type") == "变形"
                and cfg.resolve(str(r.get("crop_path", ""))).exists())
    seed_id = seed["entry_id"]
    seed_crop = cfg.resolve(str(seed["crop_path"]))
    out.append(f"种子条目: {seed_id}")
    out.append(f"种子裁剪图: {seed_crop}")
    out.append("")

    # ---------- 用例1: find-similar --entry ----------
    code, text = run_cli(["find-similar", "--entry", seed_id, "--top-k", "5"])
    out.append("=== 用例1: find-similar --entry ===")
    out.append(f"exit={code}")
    out.append(text)
    if code != 0:
        problems.append(f"用例1 期望 exit=0, 实际 {code}")
    if "形态最相似的 5 条" not in text:
        problems.append("用例1 应返回 5 条结果")
    # 只看编号结果行: 末尾的用法提示会合法地带上种子 id
    # (--similar-to-entry <seed>), 不能拿整段文本做判定
    result_lines = [ln for ln in text.splitlines()
                    if ln.strip() and ln.strip()[0].isdigit()
                    and ". d=" in ln]
    if any(seed_id in ln for ln in result_lines):
        problems.append("用例1 结果里不应包含种子自身")
    if len(result_lines) != 5:
        problems.append(f"用例1 应有 5 条结果行, 实际 {len(result_lines)}")

    # ---------- 用例2: find-similar --image 用自己的裁剪图 ----------
    # 传相对路径以顺带验证 cfg.resolve 的处理
    try:
        rel_crop = seed_crop.relative_to(cfg.base_dir).as_posix()
    except ValueError:
        rel_crop = str(seed_crop)
    code, text = run_cli(["find-similar", "--image", rel_crop, "--top-k", "3"])
    out.append("=== 用例2: find-similar --image (用种子自己的裁剪图) ===")
    out.append(f"exit={code}")
    out.append(text)
    if code != 0:
        problems.append(f"用例2 期望 exit=0, 实际 {code}")
    # 第 1 名必须是种子自身(自检逻辑, 与 check_morphology 的门槛一同理)
    first_line = ""
    for line in text.splitlines():
        if line.strip().startswith("1. d="):
            first_line = line
            break
    if seed_id not in first_line:
        problems.append(f"用例2 第1名应是种子自身, 实际: {first_line.strip()}")

    # ---------- 用例3: 缺少种子 ----------
    code, text = run_cli(["find-similar"])
    out.append("=== 用例3: find-similar 未给种子 ===")
    out.append(f"exit={code}")
    out.append(text)
    if code == 0:
        problems.append("用例3 应返回非 0")
    if "--image" not in text or "--entry" not in text:
        problems.append("用例3 应提示两种种子方式")

    # ---------- 用例4: gen-augment 方式B ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--similar-to-entry", seed_id,
                         "--similar-top-k", "4"])
    out.append("=== 用例4: gen-augment --similar-to-entry ===")
    out.append(f"exit={code}")
    out.append(text)
    if code != 0:
        problems.append(f"用例4 期望 exit=0, 实际 {code}")
    if "[方式B] 按形态相似检索" not in text:
        problems.append("用例4 应打印方式B的检索过程")
    if "最终选中参考条目 4 条" not in text:
        problems.append("用例4 应选中 4 条参考")
    if "未调用任何 API" not in text:
        problems.append("用例4 预览模式应提示未调用 API")

    # ---------- 用例5: 方式B + 方式A 组合 ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg",
                         "--similar-to-entry", seed_id,
                         "--similar-top-k", "4",
                         "--type", "划痕"])
    out.append("=== 用例5: 方式B + 方式A(--type 划痕) 组合 ===")
    out.append(f"exit={code}")
    out.append(text)
    n_scratch = sum(1 for r in records if r.get("defect_type") == "划痕")
    # 方式B 的 4 条与划痕集合可能有交集, 因此只校验并集不超过 4 + n_scratch
    # 且明确大于单独任一方式的数量
    marker = "最终选中参考条目 "
    picked = None
    for line in text.splitlines():
        if line.startswith(marker):
            picked = int(line[len(marker):].split(" 条")[0])
            break
    out.append(f"(划痕总数 {n_scratch}, 方式B 4 条, 实际选中 {picked})")
    if picked is None:
        problems.append("用例5 未能解析出选中条目数")
    elif not (n_scratch <= picked <= n_scratch + 4):
        problems.append(f"用例5 并集应在 {n_scratch}~{n_scratch + 4} 之间, "
                        f"实际 {picked}")

    # ---------- 用例6: 无挑选方式的提示包含方式B ----------
    code, text = run_cli(["gen-augment", "--classes", "1.jpg"])
    out.append("=== 用例6: gen-augment 无挑选方式 ===")
    out.append(f"exit={code}")
    out.append(text)
    if "--similar-to-image" not in text:
        problems.append("用例6 提示应包含方式B")

    # ---------- 副作用检查 ----------
    out.append("=== 副作用检查 ===")
    gen_dir = cfg.out_path("images")
    n_aug = len(list(gen_dir.rglob("*aug*.png"))) if gen_dir.exists() else 0
    out.append(f"outputs/generated 下含 aug 的文件数: {n_aug}")
    if n_aug:
        problems.append(f"预览模式不应产生 aug 产物, 实际 {n_aug} 个")

    out.append("")
    out.append("PASS" if not problems else "FAIL:\n" + "\n".join(
        f"  - {p}" for p in problems))
    (REPO / "tmp_similar_cli_result.txt").write_text(
        "\n".join(out) + "\n", encoding="utf-8")
    return 0 if not problems else 1


if __name__ == "__main__":
    raise SystemExit(main())
