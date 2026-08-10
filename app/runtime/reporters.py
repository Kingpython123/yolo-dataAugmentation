"""Reporter 的应用层实现: JSONL 事件文件、日志转发与组合。

协议与控制台实现在 src/reporting.py(核心层自带), 这里只放依赖应用层设施的实现。
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Iterator

from src import reporting

from . import logging_setup, secrets

# 事件里 log 类型的 kind 名。与结构化事件放在同一个流里, 便于界面按时间顺序
# 还原"当时发生了什么", 而不必把日志和进度分成两个来源去对齐时间。
KIND_LOG = "log"


class JsonlReporter:
    """把 log 与 event 都写成 JSON Lines, 供界面增量读取。

    为什么写文件而不是走 stdout 管道:
      worker 是分离进程, 界面可能随时关闭。若走管道, 界面关掉后没人读, worker
      写满管道缓冲区就会阻塞在 write 上, 表现为"任务莫名卡死"。写文件没有背压,
      而且界面重新打开后能直接从文件里读回全部历史, 天然支持重新附着。
    """

    def __init__(self, path: Path | str, flush_each: bool = True) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._flush_each = flush_each
        self._fh = open(self.path, "a", encoding="utf-8", newline="\n")

    # ---- Reporter 协议 ----

    def log(self, level: str, msg: str) -> None:
        self._write({"t": KIND_LOG, "level": level,
                     "msg": secrets.redact(str(msg))})

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        record = {"t": kind}
        record.update(_jsonable(payload))
        self._write(record)

    def track(self, iterable: Iterable, total: int | None = None,
              desc: str = "") -> Iterator:
        # 刻意不套 tqdm: worker 没有终端, 进度条只会往 worker.log 里灌一堆
        # 回车控制符。进度由 reporting.progress_tick 的事件承载。
        return iter(iterable)

    # ---- 内部 ----

    def _write(self, record: dict[str, Any]) -> None:
        record.setdefault("ts", time.time())
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._lock:
            self._fh.write(line)
            if self._flush_each:
                # 界面靠 tail 这个文件看进度, 不立即 flush 就会有几十秒延迟,
                # 长跑任务里那看起来就像卡住了。
                self._fh.flush()

    def close(self) -> None:
        with self._lock:
            try:
                self._fh.flush()
                self._fh.close()
            except OSError:
                pass

    def __enter__(self) -> "JsonlReporter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class LoggingReporter:
    """把 log 转到 logging(落盘 + 轮转 + 脱敏), 事件降级为 debug。"""

    _LEVELS = {
        reporting.LEVEL_INFO: 20,
        reporting.LEVEL_WARN: 30,
        reporting.LEVEL_ERROR: 40,
    }

    def __init__(self) -> None:
        self._logger = logging_setup.get_logger()

    def log(self, level: str, msg: str) -> None:
        # 核心层的消息里常带前导换行用于控制台排版, 落到日志里会多出空行
        self._logger.log(self._LEVELS.get(level, 20), str(msg).strip("\n"))

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        self._logger.debug("event %s %s", kind,
                           json.dumps(_jsonable(payload), ensure_ascii=False))

    def track(self, iterable: Iterable, total: int | None = None,
              desc: str = "") -> Iterator:
        return iter(iterable)


class TeeReporter:
    """把输出同时送往多个 reporter。

    任一实现抛异常都不应中断生成流程 —— 4 小时的任务不能因为日志文件被占用
    而失败, 所以这里吞掉子 reporter 的异常。
    """

    def __init__(self, *targets: Any) -> None:
        self._targets = [t for t in targets if t is not None]

    def log(self, level: str, msg: str) -> None:
        for t in self._targets:
            try:
                t.log(level, msg)
            except Exception:
                pass

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        for t in self._targets:
            try:
                t.event(kind, payload)
            except Exception:
                pass

    def track(self, iterable: Iterable, total: int | None = None,
              desc: str = "") -> Iterator:
        # 只让第一个可用的 reporter 包装迭代器: 多层包装会让同一个可迭代对象
        # 被消费多次
        for t in self._targets:
            try:
                return t.track(iterable, total=total, desc=desc)
            except Exception:
                continue
        return iter(iterable)


def _jsonable(value: Any) -> Any:
    """把事件负载转成可 JSON 序列化的形式。

    事件里可能夹带 Path、numpy 数值等, 直接 json.dumps 会抛异常并让整条事件丢失。
    """
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if isinstance(value, Path):
        return str(value)
    item = getattr(value, "item", None)  # numpy 标量
    if callable(item):
        try:
            return _jsonable(item())
        except Exception:
            pass
    return str(value)
