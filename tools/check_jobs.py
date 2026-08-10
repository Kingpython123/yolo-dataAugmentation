"""端到端验证作业层: 分离进程执行、事件流、优雅取消、异常收尾。

做法: 在本进程起一个只监听 127.0.0.1 的桩服务, 冒充 OpenAI 兼容中转站; 然后
真实地以分离进程启动 worker, 让它走完整的 api_client -> 生成 -> 质检 -> 落盘
链路。这样验证的是真实跨进程行为, 而不是"把 worker 当函数调一遍"。

桩服务只绑定回环地址, 不做鉴权 —— 它只在本脚本运行期间存在, 用完即关。

验证项:
  A 正常跑完   : 状态流转 pending->running->completed, 事件齐全, 产物与标注正确
  B 优雅取消   : 取消后状态为 cancelled, annotations.jsonl 行行完整, fail_log 无噪声
  C 异常收尾   : 缺陷库缺失时 worker 失败但会写明原因, 不会永远停在"运行中"
"""
from __future__ import annotations

import base64
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import numpy as np
import yaml
from PIL import Image, ImageDraw

# 干净图尺寸与暗边框宽度。留一圈暗边是为了让 segment_bottle 能识别出背景
# (它把"与画面边界连通的暗区"当背景), 同时保证裁块落在均匀的亮区里。
CLEAN_W, CLEAN_H = 600, 1200
BORDER = 24
BOTTLE_VALUE = 180
BACKGROUND_VALUE = 15

PATCH = 256

# 桩服务每次"图像编辑"的耗时。取消用例需要在任务跑着的时候插进去。
EDIT_DELAY_SECONDS = 0.6

POLL_INTERVAL = 0.5
TIMEOUT_SECONDS = 180


# --------------------------- 桩服务 ---------------------------

def _edited_patch_png() -> bytes:
    """返回"均匀亮区 + 几道暗色褶皱"的图。

    干净图的裁块是均匀的 BOTTLE_VALUE 灰, 所以这里不必知道原图内容也能保证
    差分只会命中褶皱本身。褶皱刻意画在中央, 避开 border_fade_ratio 的内缩区。
    """
    img = Image.new("RGB", (PATCH, PATCH),
                    (BOTTLE_VALUE, BOTTLE_VALUE, BOTTLE_VALUE))
    d = ImageDraw.Draw(img)
    for i in range(4):
        x = 50 + i * 40
        d.line([(x, 45), (x + 30, PATCH - 45)], fill=(70, 70, 70), width=6)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


QC_PAYLOAD = {
    "defect_present": True,
    "type_match": 8, "shape_fidelity": 7, "severity_match": 8,
    "highlight_natural": 8, "seam_continuity": 8, "blend_quality": 8,
    "only_local_change": True, "on_bottle": True,
    "artifacts": "", "verdict": "stub ok",
}


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args) -> None:  # 静音, 避免刷屏
        pass

    def _send(self, obj: dict) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 约定
        length = int(self.headers.get("Content-Length") or 0)
        # 必须读完请求体, 否则 keep-alive 连接会错位
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)

        if self.path.endswith("/images/edits"):
            time.sleep(EDIT_DELAY_SECONDS)
            png = _edited_patch_png()
            self._send({"data": [{"b64_json":
                                  base64.b64encode(png).decode("ascii")}]})
            return
        if self.path.endswith("/chat/completions"):
            self._send({"choices": [{"message": {
                "content": json.dumps(QC_PAYLOAD, ensure_ascii=False)}}]})
            return
        self._send({"error": {"message": f"stub: 未处理的路径 {self.path}"}})


def start_stub() -> tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    return server, f"http://{host}:{port}/v1"


# --------------------------- 工作区搭建 ---------------------------

def make_clean_image(path: Path) -> None:
    arr = np.full((CLEAN_H, CLEAN_W, 3), BOTTLE_VALUE, np.uint8)
    arr[:BORDER, :] = BACKGROUND_VALUE
    arr[-BORDER:, :] = BACKGROUND_VALUE
    arr[:, :BORDER] = BACKGROUND_VALUE
    arr[:, -BORDER:] = BACKGROUND_VALUE
    Image.fromarray(arr).save(path)


def build_workspace(root: Path, base_url: str, with_catalog: bool = True,
                    n_clean: int = 3) -> None:
    cfg = yaml.safe_load((REPO / "config.yaml").read_text(encoding="utf-8"))
    cfg["api"]["base_url"] = base_url
    cfg["api"]["api_key"] = "sk-stub-key-local-only"
    cfg["api"]["max_workers"] = 2
    cfg["api"]["timeout"] = 30
    cfg["api"]["http_retries"] = 0
    cfg["data"]["clean_root"] = "clean"
    cfg["data"]["defect_root"] = "defect"
    cfg["generation"]["debug"] = False
    cfg["generation"]["adaptive_patch"] = False
    cfg["generation"]["patch_size"] = PATCH
    cfg["generation"]["max_patch_size"] = PATCH
    cfg["generation"]["max_retries"] = 1
    (root / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")

    clean_dir = root / "clean" / "T1"
    clean_dir.mkdir(parents=True, exist_ok=True)
    for i in range(n_clean):
        make_clean_image(clean_dir / f"c{i}.png")

    if not with_catalog:
        return
    crops = root / "outputs" / "catalog" / "crops"
    crops.mkdir(parents=True, exist_ok=True)
    make_clean_image(crops / "ref0.png")
    catalog = [{
        "entry_id": "T1__ref0__0", "class_name": "T1", "defect_type": "变形",
        "location": "中部", "appearance": "褶皱", "severity": 4, "count": 4,
        "orientation": "斜向", "geometry": "长条", "extent": "局部",
        "photometry": "明暗交替", "edge_profile": "锐利",
        "texture_effect": "轻微弯曲", "prompt_hint": "creases",
        "source_image": "defect/T1/x.png",
        "crop_path": "outputs/catalog/crops/ref0.png",
        "bbox": [10, 10, 120, 200], "source_size": [CLEAN_W, CLEAN_H],
    }]
    (root / "outputs" / "catalog" / "catalog.json").write_text(
        json.dumps(catalog, ensure_ascii=False), encoding="utf-8")


# --------------------------- 辅助 ---------------------------

def wait_for(predicate, timeout: float = TIMEOUT_SECONDS,
             interval: float = POLL_INTERVAL) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def annotations_intact(workspace: Path) -> tuple[bool, int, str]:
    path = workspace / "outputs" / "annotations.jsonl"
    if not path.exists():
        return True, 0, ""
    count = 0
    with io.open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                json.loads(line)
            except json.JSONDecodeError as e:
                return False, count, f"第 {i} 行不可解析: {e}"
            count += 1
    return True, count, ""


def fail_log_reasons(workspace: Path) -> list[str]:
    path = workspace / "outputs" / "fail_log.jsonl"
    if not path.exists():
        return []
    out = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line).get("reason", "?"))
                except json.JSONDecodeError:
                    out.append("unparseable")
    return out


# --------------------------- 用例 ---------------------------

def case_normal(workspace: Path, base_url: str) -> list[str]:
    from app.jobs import store
    from app.services import job_service

    problems: list[str] = []
    build_workspace(workspace, base_url)

    check = job_service.preflight(["T1"], 4)
    if not check.ok:
        return [f"提交前检查未通过: {check.blockers}"]
    if check.pending != 4:
        problems.append(f"preflight.pending 期望 4, 实际 {check.pending}")

    status = job_service.submit(["T1"], 4)
    if not status.pid:
        problems.append("未记录 worker pid")
    job_dir = status.job_dir

    if not wait_for(lambda: (store.read_status(job_dir) or status).is_terminal):
        problems.append("超时: 作业未在预期时间内结束")
        log = store.worker_log_path(job_dir)
        if log.exists():
            problems.append("worker.log 尾部: "
                            + log.read_text(encoding="utf-8",
                                            errors="replace")[-800:])
        return problems

    final = store.read_status(job_dir)
    if final.state != "completed":
        problems.append(f"状态期望 completed, 实际 {final.state} ({final.error})")
    if final.stats.get("ok") != 4:
        problems.append(f"stats 期望 ok=4, 实际 {final.stats}")

    events, _ = store.read_events(job_dir, 0)
    kinds = {e.get("t") for e in events}
    for need in ("plan", "task", "progress", "finished", "log"):
        if need not in kinds:
            problems.append(f"事件流缺少 {need}")

    progress, _ = job_service.load_progress(final)
    if progress.done != 4 or progress.ok != 4:
        problems.append(f"折叠进度不符: done={progress.done} ok={progress.ok}")
    if not progress.finished or progress.status != "completed":
        problems.append(f"折叠状态不符: {progress.status}")

    intact, n, err = annotations_intact(workspace)
    if not intact:
        problems.append(f"annotations 损坏: {err}")
    elif n != 4:
        problems.append(f"annotations 期望 4 行, 实际 {n}")

    produced = list((workspace / "outputs" / "generated" / "T1").glob("*.png"))
    if len(produced) != 4:
        problems.append(f"generated 期望 4 张, 实际 {len(produced)}")
    masks = list((workspace / "outputs" / "masks" / "T1").glob("*_mask.png"))
    if len(masks) != 4:
        problems.append(f"masks 期望 4 张, 实际 {len(masks)}")
    return problems


def case_cancel(workspace: Path, base_url: str) -> list[str]:
    from app.jobs import store
    from app.services import job_service

    problems: list[str] = []
    build_workspace(workspace, base_url)

    status = job_service.submit(["T1"], 12)
    job_dir = status.job_dir

    # 等到确实开始产出后再取消, 这样才能验证"在途任务会跑完并正常落盘"
    started = wait_for(
        lambda: any(e.get("t") == "progress"
                    for e in store.read_events(job_dir, 0)[0]),
        timeout=90)
    if not started:
        problems.append("取消用例: 作业迟迟没有产出第一张, 无法验证在途落盘")

    job_service.cancel(status)

    if not wait_for(lambda: (store.read_status(job_dir) or status).is_terminal,
                    timeout=120):
        problems.append("取消超时: 作业未进入终态")
        return problems

    final = store.read_status(job_dir)
    if final.state != "cancelled":
        problems.append(f"状态期望 cancelled, 实际 {final.state} ({final.error})")
    if not final.stats.get("cancelled"):
        problems.append(f"stats.cancelled 应大于 0, 实际 {final.stats}")
    if final.stats.get("failed"):
        problems.append(f"取消不应产生失败计数, 实际 failed={final.stats.get('failed')}")

    intact, n, err = annotations_intact(workspace)
    if not intact:
        problems.append(f"取消后 annotations 损坏: {err}")
    if n == 0:
        problems.append("取消前应至少有若干张已落盘")

    reasons = fail_log_reasons(workspace)
    if reasons:
        problems.append(f"取消不应写入 fail_log, 实际: {reasons[:5]}")

    if store.cancel_requested(job_dir):
        problems.append("退出时应清理 cancel.flag")
    return problems


def case_missing_catalog(workspace: Path, base_url: str) -> list[str]:
    """缺陷库缺失: worker 必须失败并写明原因, 不能永远停在"运行中"。"""
    from app.jobs import store
    from app.jobs.spec import KIND_GEN_TARGET, JobSpec, new_job_id
    from app.jobs import launcher

    problems: list[str] = []
    build_workspace(workspace, base_url, with_catalog=False)

    # 绕过 preflight(它会提前拦住), 直接投递以验证 worker 自身的异常收尾
    spec = JobSpec(job_id=new_job_id(), kind=KIND_GEN_TARGET, classes=["T1"],
                   count=2, workspace=str(workspace),
                   clean_root=str(workspace / "clean"))
    status = launcher.launch(spec)
    job_dir = status.job_dir

    if not wait_for(lambda: (store.read_status(job_dir) or status).is_terminal,
                    timeout=90):
        problems.append("缺陷库缺失用例: 作业未进入终态(可能永远停在运行中)")
        return problems

    final = store.read_status(job_dir)
    if final.state != "failed":
        problems.append(f"状态期望 failed, 实际 {final.state}")
    if "缺陷库" not in (final.error or ""):
        problems.append(f"错误信息应指明缺陷库问题, 实际: {final.error!r}")

    events, _ = store.read_events(job_dir, 0)
    if not any(e.get("t") == "finished" for e in events):
        problems.append("异常退出也应补发 finished 事件, 否则界面会一直等")
    return problems


# --------------------------- 主流程 ---------------------------

def main() -> int:
    server, base_url = start_stub()
    print(f"桩服务: {base_url}")

    os.environ["RELAY_API_KEY"] = "sk-stub-key-local-only"
    os.environ["RELAY_BASE_URL"] = base_url

    from app.runtime import bootstrap, paths

    cases = [
        ("正常跑完(分离进程)", case_normal),
        ("优雅取消", case_cancel),
        ("缺陷库缺失时的异常收尾", case_missing_catalog),
    ]
    all_problems: list[str] = []
    try:
        for name, fn in cases:
            workspace = Path(tempfile.mkdtemp(prefix="dsjobs_"))
            os.environ[paths.WORKSPACE_ENV] = str(workspace)
            bootstrap.install(workspace=workspace)
            try:
                problems = fn(workspace, base_url)
            except Exception as e:  # noqa: BLE001 - 用例本身出错也要报出来
                import traceback
                problems = [f"用例抛出异常: {e}\n{traceback.format_exc()}"]
            finally:
                shutil.rmtree(workspace, ignore_errors=True)
            print(f"[{'PASS' if not problems else 'FAIL'}] {name}")
            for p in problems:
                print(f"        - {p}")
            all_problems.extend(problems)
    finally:
        server.shutdown()
        server.server_close()

    print()
    if all_problems:
        print(f"共 {len(all_problems)} 项不符合预期")
        return 1
    print("全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
