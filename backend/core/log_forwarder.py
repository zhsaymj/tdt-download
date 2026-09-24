"""日志转发:worker 子进程的日志经队列交回主进程统一落盘。

为什么必须做:core/logs.py 在导入时给 tdt logger 挂 RotatingFileHandler,
worker 子进程导入时会再挂一份 —— 多进程同写 data/logs/app.log 会造成行交错。
worker 侧改为只挂本模块的转发 handler,主进程收到后用自己的 logger 输出。
"""
from __future__ import annotations

import logging
import time
from multiprocessing import Queue

from .messages import EventMessage


class QueueLogHandler(logging.Handler):
    """把 LogRecord 序列化成 EventMessage.log 投进队列。"""

    def __init__(self, event_queue: Queue):
        super().__init__()
        self.event_queue = event_queue

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.event_queue.put(EventMessage.log(
                record.levelname, self.format(record), time.time()
            ))
        except Exception:
            # 日志通道故障不能拖垮任务,交给 logging 的标准兜底
            self.handleError(record)


def install_forwarding(logger: logging.Logger, event_queue: Queue) -> None:
    """把指定 logger 的 handler 全部替换为队列转发 handler。

    必须在 worker 子进程启动后立即调用,早于任何业务日志产生。
    """
    logger.handlers.clear()
    logger.addHandler(QueueLogHandler(event_queue))
    logger.setLevel(logging.INFO)
