"""日志配置: 落盘到 工作区/logs/, 按大小轮转, 并对 API key 脱敏。

约束来源:
  FR-5.6  运行日志落盘并轮转
  NFR-2   日志不得出现 key 明文
  NFR-3   长跑(4 小时以上)期间日志体积必须有界
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

from . import paths, secrets

LOGGER_NAME = "defectsynth"

MAX_BYTES = 8 * 1024 * 1024
BACKUP_COUNT = 5

FILE_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(message)s"
CONSOLE_FORMAT = "%(levelname)-7s %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class RedactFilter(logging.Filter):
    """把日志内容里的 key 形态串替换掉。

    作用在 Filter 而非 Formatter 上, 是为了同时覆盖 msg 与 args ——
    中转站的错误响应里可能回显请求头, 那部分内容并非我们自己拼接的。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = secrets.redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    k: secrets.redact(v) if isinstance(v, str) else v
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    secrets.redact(a) if isinstance(a, str) else a
                    for a in record.args)
        return True


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def setup(log_dir: Path | None = None, filename: str = "app.log",
          level: int = logging.INFO, console: bool = False) -> logging.Logger:
    """配置并返回应用 logger。重复调用是安全的(会先清空已有 handler)。

    console 默认关闭: CLI 的用户可见输出走 reporting 的 ConsoleReporter,
    日志再往控制台打一份会导致每条信息出现两次。
    """
    logger = get_logger()
    logger.setLevel(level)
    logger.propagate = False

    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        try:
            handler.close()
        except OSError:
            pass

    if log_dir is None:
        try:
            log_dir = paths.logs_dir()
        except paths.WorkspaceNotConfigured:
            # 工作区还没设好也要能记日志(引导流程本身就可能出错),
            # 退到用户配置目录下
            log_dir = paths.user_config_dir() / "logs"
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    redact = RedactFilter()

    file_handler = logging.handlers.RotatingFileHandler(
        log_dir / filename, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT,
        encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(FILE_FORMAT, DATE_FORMAT))
    file_handler.addFilter(redact)
    logger.addHandler(file_handler)

    if console and sys.stderr is not None:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        console_handler.addFilter(redact)
        logger.addHandler(console_handler)

    return logger


def current_log_file() -> Path | None:
    """当前正在写的日志文件路径, 供界面"打开日志目录"使用。"""
    for handler in get_logger().handlers:
        if isinstance(handler, logging.handlers.RotatingFileHandler):
            return Path(handler.baseFilename)
    return None
