"""日志转发:worker 子进程的日志经队列交回主进程统一落盘。

为什么必须做:core/logs.py 在导入时给 tdt logger 挂 RotatingFileHandler,
worker 子进程导入时会再挂一份 —— 多进程同写 data/logs/app.log 会造成行交错。
worker 侧改为只挂本模块的转发 handler,主进程收到后用自己的 logger 输出。

已知取舍(接受,勿"修"):子进程若硬崩溃(os._exit / 原生库段错误 / 收到信号),
多进程队列的后台 feeder 线程被直接杀死,队列中尚未刷出的日志会丢失。实测
os._exit(0) 时"崩溃前紧邻的那条日志必然丢失",幸存条数不确定(多次运行观察到
0 或 1 条,取决于启动日志是否已刷出);正常 return 时日志稳定送达。GDAL/PDAL
这类原生崩溃走的正是该路径,即在最需要日志的时刻丢尾部日志 —— 这是队列转发的
固有属性:要在崩溃时也拿到日志需改用管道/文件等同步通道。
不要为了"保住"这些日志而给子进程加 StreamHandler —— 那会让父进程把转发来的
日志再打一遍,ERROR 行重复,反而更难读。
"""
from __future__ import annotations

import logging
import time
from multiprocessing import Queue

from .messages import EventMessage


class QueueLogHandler(logging.Handler):
    """把 LogRecord 序列化成 EventMessage.log 投进队列。"""

    def __init__(self, event_queue: Queue, worker_id: str = ""):
        super().__init__()
        self.event_queue = event_queue
        self.worker_id = worker_id

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.event_queue.put(EventMessage.log(
                record.levelname, self.format(record), time.time(),
                self.worker_id,
            ))
        except Exception:
            # 日志通道故障不能拖垮任务,交给 logging 的标准兜底
            self.handleError(record)


def install_forwarding(logger: logging.Logger, event_queue: Queue,
                       worker_id: str = "") -> None:
    """把指定 logger 的 handler 全部替换为队列转发 handler。

    必须在 worker 子进程启动后立即调用,早于任何业务日志产生。

    内部先 import .logs 保证其模块级 _setup() 已执行 —— 该函数由 _tdt_ready
    守卫、只在首次导入时跑。若先 install 后 setup,业务模块首次
    ``from .logs import logger`` 会把文件 handler 重新挂上,转发静默失效。
    """
    from . import logs as _logs  # noqa: F401  触发 _setup(),幂等
    # 直接 clear 而不逐个 close():引用计数会立即回收并关闭文件句柄(无泄漏,
    # 仅 GC 时有一条 ResourceWarning)。不显式 close 是因为调用方(如测试)可能
    # 需要保存并还原 handler 列表,close 会让还原出来的是已关闭的 handler。
    logger.handlers.clear()
    logger.addHandler(QueueLogHandler(event_queue, worker_id))
    logger.setLevel(logging.INFO)
