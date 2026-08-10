"""跑 selftest / inspect, 在 Python 进程内部直接捕获 stdout 与基线比对。

不走 shell 重定向, 因为 PowerShell 5.1 的 `>` 默认写 UTF-16LE(带 BOM), 用它
产出的文件再拿 Get-Content/UTF8 读会得到乱码或解码失败, 这是编码问题而不是
程序行为问题, 之前已经踩过一次(连 baseline/*.txt 本身当时也是这样产出的,
所以读取时要按 BOM 探测编码, 且行尾统一按 \n 比较, 避免 CRLF/LF 的差异被
误判成内容差异)。这里直接在同一个进程里跑子命令并捕获 sys.stdout, 从根上
绕开 shell 的编码不确定性; 结果也写成文件而不是打印到终端, 因为 PowerShell
控制台本身按 GBK 显示, 中文 diff 打印出来一样会乱码。
"""
from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

OUT_FILE = REPO / "tmp_regression_result.txt"


def read_text_any(path: Path) -> str:
    """按 BOM 探测编码读取; 没有 BOM 就当 UTF-8。"""
    data = path.read_bytes()
    if data.startswith(b"\xff\xfe"):
        return data.decode("utf-16-le").lstrip("\ufeff")
    if data.startswith(b"\xfe\xff"):
        return data.decode("utf-16-be").lstrip("\ufeff")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    return data.decode("utf-8")


def normalize(text: str) -> str:
    return text.replace("\r\n", "\n").strip()


def run_cmd(argv: list[str]) -> str:
    from run import main
    buf = io.StringIO()
    old_argv = sys.argv
    sys.argv = ["run.py", *argv]
    try:
        with contextlib.redirect_stdout(buf):
            main()
    finally:
        sys.argv = old_argv
    return buf.getvalue()


def main_check() -> int:
    ok = True
    lines: list[str] = []
    for name in ("selftest", "inspect"):
        baseline = normalize(read_text_any(REPO / "baseline" / f"{name}.txt"))
        actual = normalize(run_cmd([name]))
        if baseline == actual:
            lines.append(f"identical: {name}")
        else:
            ok = False
            lines.append(f"DIFF: {name}")
            import difflib
            for line in difflib.unified_diff(
                    baseline.splitlines(), actual.splitlines(), lineterm=""):
                lines.append(f"  {line}")

    OUT_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main_check())
