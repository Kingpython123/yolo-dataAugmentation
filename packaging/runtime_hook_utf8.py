"""PyInstaller 运行时钩子: 在任何业务代码之前固定标准流编码为 UTF-8。

为什么不能只依赖 app/runtime/encoding.py:
  钩子在解释器启动早期执行, 而某些导入期的输出(例如依赖库的告警)发生在 main()
  之前。Windows 控制台默认代码页是 GBK, 那些输出里只要有一个非 GBK 字符就会抛
  UnicodeEncodeError, 让程序在真正跑起来之前就退出, 且错误信息本身还是乱码。
"""
import io
import os
import sys

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

for _name in ("stdout", "stderr"):
    _stream = getattr(sys, _name, None)
    if _stream is None:
        # windowed(无控制台)模式下标准流为 None, 属正常情况
        continue
    _reconfigure = getattr(_stream, "reconfigure", None)
    if _reconfigure is not None:
        try:
            _reconfigure(encoding="utf-8", errors="replace")
            continue
        except (ValueError, OSError):
            pass
    _buffer = getattr(_stream, "buffer", None)
    if _buffer is not None:
        try:
            setattr(sys, _name,
                    io.TextIOWrapper(_buffer, encoding="utf-8",
                                     errors="replace", line_buffering=True))
        except (ValueError, OSError):
            pass
