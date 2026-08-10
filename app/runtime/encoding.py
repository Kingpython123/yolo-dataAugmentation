"""强制标准流为 UTF-8。

Windows 控制台默认代码页是 GBK(cp936), 项目里所有中文提示、缺陷库描述、
失败日志都是 UTF-8。不固定编码会出现两类问题:
  1. 输出到控制台时中文变乱码(实测已发生)
  2. 遇到 GBK 无法表示的字符(如 → ≥ 等)直接抛 UnicodeEncodeError 中断流程

打包后的程序无法依赖用户预先设置 PYTHONUTF8/PYTHONIOENCODING, 因此在进程入口
显式重配置。CLI、worker、GUI 三个入口都必须先调用 configure_stdio()。
"""
from __future__ import annotations

import sys

ENCODING = "utf-8"

# 遇到无法编码的字符时替换而不是抛异常: 日志可读性优先于字符保真,
# 绝不能因为一个字符导致 4 小时的任务中断。
ERRORS = "replace"


def configure_stdio() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            # GUI(windowed)模式下标准流可能是 None, 属正常情况
            continue
        try:
            reconfigure(encoding=ENCODING, errors=ERRORS)
        except (ValueError, OSError):
            pass
