"""核心层的输出边界。

背景
----
改造前核心层直接 print + tqdm, 而且 print 发生在 ThreadPoolExecutor 的工作
线程里。这带来两个问题:
  1. 图形界面拿不到进度 —— 除非去劫持 sys.stdout, 那是脆弱且难调试的做法
  2. 关键诊断信息随终端滚动永久丢失(实测出现过 58% 失败率却查不到原因的情况)

因此在核心层引入一个薄边界: 核心层只调用本模块的 info/warn/event/track,
由适配器决定这些信息去哪里 —— CLI 走控制台(保持原样), worker 走事件文件,
两者都可以同时再落一份日志。

为什么放在 src/ 而不是 app/
--------------------------
依赖方向必须是 app -> src 单向。若把协议放在 app/ 下, src 就得反向 import,
核心层从此离不开应用层。所以协议与默认的控制台实现留在核心层, 其余实现
(JSONL/日志/组合)放在 app/runtime/reporters.py。

默认行为
--------
默认 reporter 是 ConsoleReporter, 它的 log 就是 print、track 就是 tqdm,
因此不设置任何东西时 CLI 输出与改造前逐字一致。

线程安全
--------
核心层的生成流程是多线程的, 因此 reporter 实现必须自己保证 log/event 的线程
安全。ConsoleReporter 依赖 print 本身的原子性(CPython 下单次 print 不会交错),
需要更强保证的实现(如写文件)自行加锁。
"""
from __future__ import annotations

from typing import Any, Iterable, Iterator, Protocol, runtime_checkable

LEVEL_INFO = "info"
LEVEL_WARN = "warn"
LEVEL_ERROR = "error"

# 事件类型常量。核心层只负责发出, 语义定义见 design.md 6.3
EVENT_PLAN = "plan"
EVENT_TASK = "task"
EVENT_PROGRESS = "progress"
EVENT_STATS = "stats"
EVENT_FINISHED = "finished"


@runtime_checkable
class Reporter(Protocol):
    """输出接收端。三个方法对应三类信息, 不要混用。"""

    def log(self, level: str, msg: str) -> None:
        """给人看的一行文本。"""

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        """给程序看的结构化事件。"""

    def track(self, iterable: Iterable, total: int | None = None,
              desc: str = "") -> Iterator:
        """包装一个可迭代对象以汇报进度; 必须原样逐项 yield。"""


class ConsoleReporter:
    """控制台实现: 与改造前的 print + tqdm 行为一致。"""

    def log(self, level: str, msg: str) -> None:
        print(msg)

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        # 结构化事件对 CLI 用户没有阅读价值(同样的信息已由 log 打印过),
        # 这里刻意丢弃, 以保证 CLI 输出与改造前逐字相同。
        pass

    def track(self, iterable: Iterable, total: int | None = None,
              desc: str = "") -> Iterator:
        from tqdm import tqdm
        return iter(tqdm(iterable, total=total, desc=desc))


class NullReporter:
    """全部丢弃。用于不希望产生任何输出的场合。"""

    def log(self, level: str, msg: str) -> None:
        pass

    def event(self, kind: str, payload: dict[str, Any]) -> None:
        pass

    def track(self, iterable: Iterable, total: int | None = None,
              desc: str = "") -> Iterator:
        return iter(iterable)


_reporter: Reporter = ConsoleReporter()


def set_reporter(reporter: Reporter | None) -> Reporter:
    """替换当前 reporter, 返回被替换掉的那个(便于调用方恢复)。"""
    global _reporter
    previous = _reporter
    _reporter = reporter if reporter is not None else ConsoleReporter()
    return previous


def get_reporter() -> Reporter:
    return _reporter


# --------------------------- 便捷函数 ---------------------------
# 核心层统一调用这一组, 不直接接触 reporter 实例。

def log(level: str, msg: str) -> None:
    _reporter.log(level, msg)


def info(msg: str) -> None:
    _reporter.log(LEVEL_INFO, msg)


def warn(msg: str) -> None:
    _reporter.log(LEVEL_WARN, msg)


def error(msg: str) -> None:
    _reporter.log(LEVEL_ERROR, msg)


def event(kind: str, **fields: Any) -> None:
    _reporter.event(kind, fields)


def track(iterable: Iterable, total: int | None = None,
          desc: str = "") -> Iterator:
    return _reporter.track(iterable, total=total, desc=desc)


def progress_tick(done: int, total: int) -> None:
    """显式上报进度。

    与 track() 分开是因为 track 包裹的是 as_completed(...), 它只知道"有一个
    future 返回了", 分不清那是真正完成的任务还是被取消跳过的任务。进度条允许
    这点误差, 但界面上的"已完成 N/M"不允许, 所以由调用方在确认完成后显式上报。
    """
    _reporter.event(EVENT_PROGRESS, {"done": done, "total": total})
