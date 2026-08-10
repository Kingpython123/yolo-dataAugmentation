"""一次性迁移: run.py 接入 bootstrap 装配, cmd_inspect 改用结构化体检报告。

同样采用"全有或全无"策略, 并显式用 utf-8 读写以避开本机的编码陷阱。
"""
from __future__ import annotations

import sys
from pathlib import Path

TARGET = Path(__file__).resolve().parents[1] / "run.py"

EDITS: list[tuple[str, str, str]] = [
    (
        "模块 docstring 补充新增子命令",
        'from __future__ import annotations\n'
        '\n'
        'import argparse\n'
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
        '\n'
        'from src.config import load_config\n',

        'from __future__ import annotations\n'
        '\n'
        'import argparse\n'
        'import sys\n'
        'from pathlib import Path\n'
        '\n'
        'sys.path.insert(0, str(Path(__file__).resolve().parent))\n'
        '\n'
        'from app.runtime import bootstrap\n'
        'from src.config import load_config\n',
    ),
    (
        "cmd_inspect 改用 catalog_report",
        'def cmd_inspect(args):\n'
        '    """离线体检缺陷库: 格式/类型/严重度/字段完整度/裁剪图是否存在。"""\n'
        '    import json\n'
        '    from collections import Counter\n'
        '    from pathlib import Path as P\n'
        '    cfg = load_config(args.config)\n'
        '    f = cfg.out_path("catalog") / "catalog.json"\n'
        '    if not f.exists():\n'
        '        print(f"缺陷库不存在: {f}")\n'
        '        return\n'
        '    recs = json.loads(f.read_text(encoding="utf-8"))\n'
        '    print(f"缺陷库: {f}\\n条目总数: {len(recs)}")\n'
        '\n'
        '    new = [r for r in recs if "severity" in r]\n'
        '    print(f"新格式(含细化字段): {len(new)} / 旧格式: {len(recs) - len(new)}")\n'
        '    print("按类型:", dict(Counter(r.get("defect_type") for r in recs)))\n'
        '    print("按严重度:", dict(sorted(Counter(\n'
        '        r.get("severity", "?") for r in recs).items(), key=lambda x: str(x[0]))))\n'
        '    print("按类别:", dict(sorted(Counter(r.get("class_name") for r in recs).items())))\n'
        '\n'
        '    white = (cfg.get("catalog", {}) or {}).get("defect_types", ["变形", "划痕"])\n'
        '    bad_type = [r["entry_id"] for r in recs if r.get("defect_type") not in white]\n'
        '    missing = [r["entry_id"] for r in recs\n'
        '               if not cfg.resolve(str(r.get("crop_path", ""))).exists()]\n'
        '    no_size = [r["entry_id"] for r in recs\n'
        '               if not isinstance(r.get("source_size"), (list, tuple))]\n'
        '    fields = ["orientation", "geometry", "extent", "photometry",\n'
        '              "edge_profile", "texture_effect", "prompt_hint"]\n'
        '    empty = [r["entry_id"] for r in recs\n'
        '             if sum(1 for k in fields if not str(r.get(k, "")).strip()) > 2]\n'
        '    print(f"\\n非白名单类型: {len(bad_type)}", bad_type[:5])\n'
        '    print(f"裁剪图缺失: {len(missing)}", missing[:5])\n'
        '    if no_size:\n'
        '        print(f"[警告] 缺 source_size 的条目: {len(no_size)} 条 —— 这些条目在"\n'
        '              f"没有\'有缺陷\'原图的机器上会退回按绝对像素算裁块(尺寸偏大)")\n'
        '    print(f"描述字段大量缺失: {len(empty)}", empty[:5])\n'
        '\n'
        '    big = [(r["entry_id"], r["bbox"][2], r["bbox"][3]) for r in recs\n'
        '           if r.get("bbox") and max(r["bbox"][2], r["bbox"][3])\n'
        '           > cfg.generation.get("max_patch_size", 1536)]\n'
        '    if big:\n'
        '        print(f"\\n[提示] {len(big)} 条缺陷长边超过 max_patch_size, 裁块无法完整覆盖:")\n'
        '        for e, w, h in big[:5]:\n'
        '            print(f"   {e}  bbox={w}x{h}")\n',

        'def cmd_inspect(args):\n'
        '    """离线体检缺陷库: 格式/类型/严重度/字段完整度/裁剪图是否存在。\n'
        '\n'
        '    判定逻辑已提炼到 src/catalog_report.py, 图形界面复用同一份结果;\n'
        '    这里只负责把它渲染成控制台文本。\n'
        '    """\n'
        '    from src import catalog_report\n'
        '    cfg = load_config(args.config)\n'
        '    report = catalog_report.build_report(cfg)\n'
        '    for line in catalog_report.format_lines(report):\n'
        '        print(line)\n',
    ),
    (
        "main(): 先装配运行环境",
        '    args = ap.parse_args()\n'
        '    args.func(args)\n',

        '    args = ap.parse_args()\n'
        '    # 装配运行环境: 固定标准流编码、把默认 config.yaml 指向工作区、\n'
        '    # 注入凭据管理器里的 API key。源码方式运行且未设置工作区时,\n'
        '    # 工作区回退为仓库根目录, 行为与改造前一致。\n'
        '    bootstrap.install(workspace=getattr(args, "workspace", None))\n'
        '    args.func(args)\n',
    ),
    (
        "全局参数: 新增 --workspace",
        '    ap = argparse.ArgumentParser(description="工业瓶子缺陷合成流水线")\n'
        '    ap.add_argument("--config", default=None, help="config.yaml 路径")\n',

        '    ap = argparse.ArgumentParser(description="工业瓶子缺陷合成流水线")\n'
        '    ap.add_argument("--config", default=None, help="config.yaml 路径")\n'
        '    ap.add_argument("--workspace", default=None,\n'
        '                    help="工作区目录(存放 config.yaml/outputs/logs/jobs);"\n'
        '                         "默认取用户配置, 源码运行时回退为仓库根目录")\n',
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
