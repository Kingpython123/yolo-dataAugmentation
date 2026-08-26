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


def check_flat_edit_defaults() -> list[str]:
    """config.yaml 的 flat-edit 参数键 与 mask_utils 里的默认值常量 是否一致。

    背景: 曾经把 highlight_knee 的默认值同时写在"函数签名"和"config.yaml"
    两处, 调参时只改了两处、漏了 generate.py 里 gen.get() 的第三处兜底值,
    结果 config 该 key 一旦缺失或拼错就会静默回退到一个已被实测否决的旧值
    (235, 而非当时定的 252), 且不报错, 很难被发现。
    修复方式是把默认值集中定义成 mask_utils 里的常量, 函数签名与调用处都
    引用同一个常量。这里的检查确保三处(常量 / 签名 / config.yaml)不再各说
    各话 —— 一旦有人改常量却忘了同步 config, 或反过来, 立刻报出来。
    """
    import inspect as _inspect

    from src import mask_utils
    from src.config import load_config

    # (config.yaml 里的 key, mask_utils 里的常量名, 引用该默认值的函数, 形参名)
    checks = [
        ("flat_sigma_ratio", "FLAT_SIGMA_RATIO", mask_utils.flat_sigma, "ratio"),
        ("flat_sigma_min", "FLAT_SIGMA_MIN", mask_utils.flat_sigma, "lo"),
        ("flat_sigma_max", "FLAT_SIGMA_MAX", mask_utils.flat_sigma, "hi"),
        ("ratio_eps_ratio", "RATIO_EPS_RATIO", mask_utils.shading_log_ratio, "eps_ratio"),
        ("ratio_eps_floor", "RATIO_EPS_FLOOR", mask_utils.shading_log_ratio, "eps_floor"),
        ("ratio_log_clamp_lo", "RATIO_LOG_CLAMP_LO", mask_utils.shading_log_ratio, "clamp_lo"),
        ("ratio_log_clamp_hi", "RATIO_LOG_CLAMP_HI", mask_utils.shading_log_ratio, "clamp_hi"),
        ("ratio_highlight_knee", "RATIO_HIGHLIGHT_KNEE", mask_utils.ratio_composite, "highlight_knee"),
        ("ratio_mask_thresh", "RATIO_MASK_THRESH", mask_utils.mask_from_log_ratio, "thresh"),
    ]

    gen = load_config().generation
    problems = []
    for cfg_key, const_name, fn, param in checks:
        const_val = getattr(mask_utils, const_name)
        sig_val = _inspect.signature(fn).parameters[param].default
        cfg_val = gen.get(cfg_key)

        if sig_val != const_val:
            problems.append(
                f"{fn.__name__}() 形参 {param} 的默认值 {sig_val!r} "
                f"与 mask_utils.{const_name} = {const_val!r} 不一致")
        if cfg_val is None:
            problems.append(f"config.yaml 缺少 generation.{cfg_key}"
                            f"(缺失时会回退到常量 {const_val!r}, 请确认这是预期的)")
        elif abs(float(cfg_val) - float(const_val)) > 1e-9:
            problems.append(
                f"config.yaml 的 generation.{cfg_key} = {cfg_val!r} "
                f"与 mask_utils.{const_name} = {const_val!r} 不一致 "
                f"(若是故意覆盖默认值可忽略, 若不是请检查是否调参时漏改了某一处)")
    return problems


def main_check() -> int:
    ok = True
    lines: list[str] = []

    flat_edit_problems = check_flat_edit_defaults()
    if flat_edit_problems:
        ok = False
        lines.append("DIFF: flat_edit_defaults")
        for p in flat_edit_problems:
            lines.append(f"  {p}")
    else:
        lines.append("identical: flat_edit_defaults")

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
