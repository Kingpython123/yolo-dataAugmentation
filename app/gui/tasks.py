"""把耗时的服务调用放到线程池, 保持界面不卡。

哪些操作需要走这里: 数据集扫描(要遍历上千文件)、缺陷库体检(要 stat 674 个文件)、
连通性测试(要等两次大模型调用)、导出打包(要压缩几万张图)、缺陷库安装(440 MB 解压)。

生成任务不在此列 —— 它跑在分离的 worker 进程里, 见 app/jobs/launcher.py。

关于 QRunnable 的生命周期(踩过的坑)
----------------------------------
QRunnable 默认 autoDelete=True: run() 返回后线程池会立刻删除它。而承载信号的
QObject 是这个 Python 对象的属性, 调用方又通常不保存 submit() 的返回值 —— 于是
Python 侧失去最后一个引用, 对象被回收, 已经排队等待投递到主线程的信号就此丢失。
症状是回调时而触发时而不触发, 界面上表现为"某个页面的数据偶尔加载不出来"。

因此这里显式关掉 autoDelete, 并把任务挂在模块级集合里, 直到 done 信号送达主线程
后才移除。
"""
from __future__ import annotations

import traceback
from typing import Any, Callable

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal, Slot


class TaskSignals(QObject):
    """QRunnable 自己不能带信号, 因此单独包一个 QObject。"""

    finished = Signal(object)          # 成功结果
    failed = Signal(str)               # 人可读的错误信息
    progress = Signal(int, int, str)   # 已完成, 总数, 说明
    done = Signal()                    # 无论成败都会发


class Task(QRunnable):
    """执行一个可调用对象并把结果送回主线程。

    若目标函数接受 progress 关键字参数, 会自动注入一个转发到信号的回调, 这样
    服务层不需要知道 Qt 的存在。
    """

    def __init__(self, fn: Callable[..., Any], *args,
                 wants_progress: bool = False, **kwargs) -> None:
        super().__init__()
        # 见模块 docstring: 交给线程池自动删除会让排队中的信号丢失
        self.setAutoDelete(False)
        self._fn = fn
        self._args = args
        self._kwargs = kwargs
        self._wants_progress = wants_progress
        self.signals = TaskSignals()

    @Slot()
    def run(self) -> None:
        try:
            kwargs = dict(self._kwargs)
            if self._wants_progress:
                kwargs["progress"] = self._emit_progress
            result = self._fn(*self._args, **kwargs)
        except Exception as e:  # noqa: BLE001 - 任何异常都要回到界面, 不能吞
            self.signals.failed.emit(_readable(e))
        else:
            self.signals.finished.emit(result)
        finally:
            self.signals.done.emit()

    def _emit_progress(self, done: int, total: int, text: str = "") -> None:
        self.signals.progress.emit(int(done), int(total), str(text))


# 在途任务的强引用。key 用 id() 只是为了避免依赖对象可哈希性以外的假设。
_active: dict[int, Task] = {}


def active_count() -> int:
    """在途任务数。给自检脚本用, 避免它在任务还没回来时就截图。"""
    return len(_active)


def _readable(exc: Exception) -> str:
    """错误信息优先给用户看得懂的那句, 完整栈只写日志。"""
    from app.runtime import logging_setup, secrets

    logging_setup.get_logger().error("后台任务失败: %s\n%s", exc,
                                     traceback.format_exc())
    message = str(exc).strip() or exc.__class__.__name__
    return secrets.redact(message)


def submit(fn: Callable[..., Any], *args, on_success=None, on_error=None,
           on_progress=None, on_done=None, wants_progress: bool = False,
           **kwargs) -> Task:
    """提交一个后台任务并挂上回调。"""
    task = Task(fn, *args, wants_progress=wants_progress, **kwargs)
    if on_success is not None:
        task.signals.finished.connect(on_success)
    if on_error is not None:
        task.signals.failed.connect(on_error)
    if on_progress is not None:
        task.signals.progress.connect(on_progress)
    if on_done is not None:
        task.signals.done.connect(on_done)

    # 释放引用的连接必须最后挂: 槽按连接顺序执行, 提前释放会让后面的槽拿到
    # 已被回收的对象。
    key = id(task)
    _active[key] = task
    task.signals.done.connect(lambda: _active.pop(key, None))

    QThreadPool.globalInstance().start(task)
    return task
