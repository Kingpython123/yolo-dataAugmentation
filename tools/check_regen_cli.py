"""通过真实 CLI 入口(run.py)跑 regen-rejected --dry-run, 捕获 stdout 落盘查看。

与 tools/check_regen_plan.py 的区别: 那个直接调用内部函数验证规划器本身的正确
性(已过); 这个验证 CLI 参数解析 -> cmd_regen_rejected -> 打印格式 这条完整链路,
即用户敲这行命令时实际会看到什么。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def main() -> int:
    from run import main as run_main

    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["run.py", "regen-rejected", "--dry-run"]
    try:
        with contextlib.redirect_stdout(buf):
            code = run_main()
    finally:
        sys.argv = old_argv

    output = buf.getvalue()
    (REPO / "tmp_regen_cli_result.txt").write_text(
        f"exit_code={code}\n\n{output}", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
