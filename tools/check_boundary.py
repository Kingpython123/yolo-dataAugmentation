"""离线验证输出边界、结构化事件与优雅取消, 不调用任何真实 API。

做法: 用一个假的 RelayClient 顶替真实客户端, 在合成图上跑完整的 _run_tasks,
然后检查
  1. 事件文件里是否按预期出现 plan / task / progress / finished
  2. cancel 置位后未启动的任务是否被跳过、且没写进 fail_log
  3. annotations.jsonl 是否行行完整可解析(取消不得写坏它)

这三条正是设计文档里标为"高风险"的项, 不能只靠肉眼看界面来确认。
"""
from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np
from PIL import Image, ImageDraw

from app.runtime import reporters
from src import generate, reporting


class FakeRelay:
    """返回"在裁块上画了几道褶皱"的图, 并放行质检。"""

    def __init__(self, cfg):
        self.base_url = "fake://local"
        self.models = cfg.models

    def edit_image(self, prompt, base_image, references=None, mask=None,
                   model=None):
        out = base_image.copy()
        d = ImageDraw.Draw(out)
        w, h = out.size
        for i in range(4):
            x = int(w * (0.2 + 0.15 * i))
            d.line([(x, int(h * 0.2)), (x + int(w * 0.08), int(h * 0.8))],
                   fill=(70, 70, 70), width=max(3, w // 90))
        return out

    def chat_vision(self, prompt, images=None, model=None, temperature=0.2,
                    expect_json=False):
        return json.dumps({
            "type_match": 8, "shape_fidelity": 7, "severity_match": 8,
            "highlight_natural": 8, "seam_continuity": 8, "blend_quality": 8,
            "defect_present": True, "only_local_change": True,
            "on_bottle": True,
            "comment": "fake",
        }, ensure_ascii=False)


def make_clean_image(path: Path) -> None:
    arr = np.full((1200, 600, 3), 20, np.uint8)
    arr[100:1100, 200:400] = 180
    Image.fromarray(arr).save(path)


def build_config(root: Path):
    """在临时目录里造一个自洽的工作区: config + 缺陷库 + 干净图。"""
    import yaml

    from src import config as core_config

    base = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    base["api"]["api_key"] = "sk-fake-key-for-offline-check"
    base["api"]["max_workers"] = 2
    base["data"]["clean_root"] = "clean"
    base["data"]["defect_root"] = "defect"
    base["generation"]["debug"] = False
    base["generation"]["patch_size"] = 256
    base["generation"]["max_patch_size"] = 320
    base["generation"]["adaptive_patch"] = False
    base["generation"]["max_retries"] = 1
    cfg_path = root / "config.yaml"
    cfg_path.write_text(yaml.safe_dump(base, allow_unicode=True),
                        encoding="utf-8")

    # 干净图
    clean_dir = root / "clean" / "T1"
    clean_dir.mkdir(parents=True)
    for i in range(3):
        make_clean_image(clean_dir / f"c{i}.png")

    # 缺陷库: 一条参考, 裁剪图用干净图凑
    crops = root / "outputs" / "catalog" / "crops"
    crops.mkdir(parents=True)
    ref_crop = crops / "ref0.png"
    make_clean_image(ref_crop)
    catalog = [{
        "entry_id": "T1__ref0__0", "class_name": "T1", "defect_type": "变形",
        "location": "中部", "appearance": "褶皱", "severity": 4, "count": 4,
        "orientation": "斜向", "geometry": "长条", "extent": "局部",
        "photometry": "明暗交替", "edge_profile": "锐利",
        "texture_effect": "轻微弯曲", "prompt_hint": "creases",
        "source_image": "defect/T1/x.png",
        "crop_path": "outputs/catalog/crops/ref0.png",
        "bbox": [10, 10, 120, 200], "source_size": [600, 1200],
    }]
    (root / "outputs" / "catalog" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8")

    return core_config.load_config(cfg_path)


def read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def scenario_normal(root: Path) -> list[str]:
    """正常跑完: 检查事件齐全、annotations 可解析。"""
    problems: list[str] = []
    cfg = build_config(root)
    generate.RelayClient = FakeRelay  # type: ignore[assignment]

    events_path = root / "events.jsonl"
    rep = reporters.JsonlReporter(events_path)
    prev = reporting.set_reporter(rep)
    try:
        generate.generate_target(cfg, ["T1"], count=4, resume=False)
    finally:
        reporting.set_reporter(prev)
        rep.close()

    events = read_events(events_path)
    kinds = [e["t"] for e in events]
    for need in ("plan", "task", "progress", "finished"):
        if need not in kinds:
            problems.append(f"缺少事件: {need}")

    plan = next((e for e in events if e["t"] == "plan"), None)
    if plan and plan.get("pending") != 4:
        problems.append(f"plan.pending 期望 4, 实际 {plan.get('pending')}")

    tasks = [e for e in events if e["t"] == "task"]
    if len(tasks) != 4:
        problems.append(f"task 事件期望 4 条, 实际 {len(tasks)}")
    if any(t.get("verdict") != "ok" for t in tasks):
        problems.append(f"verdict 非全 ok: {[t.get('verdict') for t in tasks]}")

    fin = next((e for e in events if e["t"] == "finished"), None)
    if fin and fin.get("status") != "completed":
        problems.append(f"finished.status 期望 completed, 实际 {fin.get('status')}")
    if fin and fin.get("stats", {}).get("ok") != 4:
        problems.append(f"stats.ok 期望 4, 实际 {fin.get('stats')}")

    ann = root / "outputs" / "annotations.jsonl"
    lines = [l for l in ann.read_text(encoding="utf-8").splitlines() if l.strip()]
    if len(lines) != 4:
        problems.append(f"annotations 期望 4 行, 实际 {len(lines)}")
    for i, l in enumerate(lines):
        try:
            json.loads(l)
        except json.JSONDecodeError as e:
            problems.append(f"annotations 第 {i+1} 行不可解析: {e}")

    n_generated = len(list((root / "outputs" / "generated" / "T1").glob("*.png")))
    if n_generated != 4:
        problems.append(f"generated 期望 4 张, 实际 {n_generated}")
    return problems


def scenario_cancel(root: Path) -> list[str]:
    """一开始就置位取消: 全部任务应被跳过, 不产生失败日志, 不写坏 annotations。"""
    problems: list[str] = []
    cfg = build_config(root)
    generate.RelayClient = FakeRelay  # type: ignore[assignment]

    events_path = root / "events.jsonl"
    rep = reporters.JsonlReporter(events_path)
    prev = reporting.set_reporter(rep)
    cancel = threading.Event()
    cancel.set()
    try:
        generate.generate_target(cfg, ["T1"], count=5, resume=False,
                                 cancel=cancel)
    finally:
        reporting.set_reporter(prev)
        rep.close()

    events = read_events(events_path)
    fin = next((e for e in events if e["t"] == "finished"), None)
    if fin is None:
        problems.append("取消场景缺少 finished 事件")
    else:
        if fin.get("status") != "cancelled":
            problems.append(f"finished.status 期望 cancelled, 实际 {fin.get('status')}")
        stats = fin.get("stats", {})
        if stats.get("cancelled") != 5:
            problems.append(f"stats.cancelled 期望 5, 实际 {stats}")
        if stats.get("failed"):
            problems.append(f"取消不应计入失败, 实际 failed={stats.get('failed')}")

    if [e for e in events if e["t"] == "task"]:
        problems.append("取消场景不应产生 task 事件")

    fail_log = root / "outputs" / "fail_log.jsonl"
    if fail_log.exists() and fail_log.read_text(encoding="utf-8").strip():
        problems.append("取消场景不应写 fail_log.jsonl")

    ann = root / "outputs" / "annotations.jsonl"
    if ann.exists():
        for i, l in enumerate(ann.read_text(encoding="utf-8").splitlines()):
            if l.strip():
                try:
                    json.loads(l)
                except json.JSONDecodeError as e:
                    problems.append(f"annotations 第 {i+1} 行损坏: {e}")
    return problems


def scenario_console_silent_events(root: Path) -> list[str]:
    """ConsoleReporter 必须丢弃结构化事件, 以保证 CLI 输出与改造前逐字一致。"""
    problems: list[str] = []
    buf = io.StringIO()
    prev_stdout, sys.stdout = sys.stdout, buf
    prev = reporting.set_reporter(reporting.ConsoleReporter())
    try:
        reporting.event("plan", total=1, skipped=0, pending=1)
        reporting.progress_tick(1, 1)
        reporting.info("可见文本")
    finally:
        reporting.set_reporter(prev)
        sys.stdout = prev_stdout
    out = buf.getvalue()
    if out != "可见文本\n":
        problems.append(f"ConsoleReporter 输出应仅含 log, 实际: {out!r}")
    return problems


def main() -> int:
    all_problems: list[str] = []
    checks = [
        ("正常跑完 + 事件齐全", scenario_normal),
        ("优雅取消", scenario_cancel),
        ("Console 事件静默", scenario_console_silent_events),
    ]
    for name, fn in checks:
        with tempfile.TemporaryDirectory(prefix="dsboundary_") as td:
            problems = fn(Path(td))
        status = "PASS" if not problems else "FAIL"
        print(f"[{status}] {name}")
        for p in problems:
            print(f"        - {p}")
        all_problems.extend(problems)

    print()
    if all_problems:
        print(f"共 {len(all_problems)} 项不符合预期")
        return 1
    print("全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
