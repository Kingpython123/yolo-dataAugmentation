"""后台执行进程入口。

由界面(或 CLI 的 `worker` 子命令)以分离进程方式启动, 负责跑一个作业并把过程
写进作业目录。它是唯一会调用 src.generate 的地方 —— 界面进程本身从不执行生成,
这样界面崩溃或被关闭都不会影响正在跑的任务。
"""
from __future__ import annotations

import threading
import time
import traceback
from pathlib import Path
from typing import Any

from app.jobs import store
from app.jobs.spec import (STATE_CANCELLED, STATE_COMPLETED, STATE_FAILED,
                           STATE_RUNNING, KIND_GEN_TARGET, JobSpec)
from app.runtime import bootstrap, reporters
from src import reporting

# 轮询 cancel.flag 的间隔。取消不需要毫秒级响应, 但要明显快于单张耗时(50~60 秒),
# 否则用户点了停止还要等很久才看到反应。
CANCEL_POLL_SECONDS = 1.0

# 心跳间隔。界面据此判断"进程是不是还活着", 需明显小于 HEARTBEAT_TIMEOUT。
HEARTBEAT_SECONDS = 10.0


class _StatsCapture:
    """从事件流里截获收尾统计, 供写最终状态用。

    不另外维护一套计数: 事件里的 stats 就是生成流程自己的权威统计, 再数一遍
    只会引入不一致。
    """

    def __init__(self) -> None:
        self.stats: dict[str, Any] = {}
        self.finish_status = ""

    def log(self, level: str, msg: str) -> None:
        pass

    def event(self, kind: str, payload: dict) -> None:
        if kind == reporting.EVENT_FINISHED:
            raw = payload.get("stats")
            if isinstance(raw, dict):
                self.stats = dict(raw)
            self.finish_status = str(payload.get("status") or "")

    def track(self, iterable, total=None, desc=""):
        return iter(iterable)


def _start_cancel_watcher(job_directory: Path, cancel: threading.Event,
                          stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.wait(CANCEL_POLL_SECONDS):
            if store.cancel_requested(job_directory):
                if not cancel.is_set():
                    reporting.info("[cancel] 收到停止请求: 不再启动新任务, "
                                   "等待在途任务落盘后退出")
                    cancel.set()
                return

    t = threading.Thread(target=loop, name="cancel-watch", daemon=True)
    t.start()
    return t


def _start_heartbeat(job_directory: Path, stop: threading.Event) -> threading.Thread:
    def loop() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            try:
                store.touch_heartbeat(job_directory)
            except OSError:
                pass

    t = threading.Thread(target=loop, name="heartbeat", daemon=True)
    t.start()
    return t


def _apply_spec_overrides(cfg, spec: JobSpec) -> None:
    """把作业级参数覆盖到配置上。

    这是配置分层的最后一级(见 requirements.md FR-3.1): 界面本次运行的选择优先
    于工作区 config.yaml。直接改 Config 持有的字典即可, 不落盘, 因此不会污染
    用户手工维护的 config.yaml。
    """
    if spec.max_workers:
        cfg.api["max_workers"] = int(spec.max_workers)
    if spec.clean_root:
        cfg.data["clean_root"] = spec.clean_root


def run_job(job_directory: Path | str) -> int:
    """执行一个作业。返回进程退出码。"""
    job_directory = Path(job_directory)
    spec = store.read_spec(job_directory)
    if spec is None:
        print(f"作业参数不可读: {job_directory}")
        return 2

    bootstrap.install(workspace=spec.workspace or None,
                      log_name=f"worker-{spec.job_id}.log")

    jsonl = reporters.JsonlReporter(store.events_path(job_directory))
    capture = _StatsCapture()
    reporting.set_reporter(reporters.TeeReporter(
        jsonl, reporters.LoggingReporter(), capture))

    cancel = threading.Event()
    stop = threading.Event()
    _start_cancel_watcher(job_directory, cancel, stop)
    _start_heartbeat(job_directory, stop)

    store.update_status(job_directory, state=STATE_RUNNING,
                        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
                        heartbeat=time.time())

    state = STATE_COMPLETED
    error = ""
    try:
        # 延迟导入: generate 会拉起 cv2/numpy, 放在这里可以让"参数不可读"之类的
        # 早期错误更快返回, 也让 worker 的启动失败更容易定位。
        from src.config import load_config
        from src.generate import generate_target

        cfg = load_config()
        _apply_spec_overrides(cfg, spec)

        reporting.info(f"[worker] 作业 {spec.job_id} 开始: {spec.description}")
        if spec.kind == KIND_GEN_TARGET:
            generate_target(cfg, spec.classes, count=spec.count,
                            resume=spec.resume, cancel=cancel)
        else:
            raise ValueError(f"不支持的作业类型: {spec.kind}")

        if cancel.is_set() or capture.finish_status == "cancelled":
            state = STATE_CANCELLED
    except BaseException as e:  # noqa: BLE001 - 任何异常都要写进状态再退出
        state = STATE_FAILED
        error = f"{type(e).__name__}: {e}"
        reporting.error(f"[worker] 作业失败: {error}")
        reporting.error(traceback.format_exc())
    finally:
        stop.set()
        store.update_status(
            job_directory, state=state, error=error,
            finished_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
            heartbeat=time.time(), stats=capture.stats)
        # finished 事件由 generate 发出; 作业异常退出时补一条, 否则界面会一直
        # 停在"运行中"等一个永远不来的收尾事件。
        if state == STATE_FAILED:
            reporting.event(reporting.EVENT_FINISHED, status="error",
                            stats=capture.stats, error=error)
        reporting.set_reporter(None)
        jsonl.close()
        store.clear_cancel(job_directory)

    return 0 if state != STATE_FAILED else 1
