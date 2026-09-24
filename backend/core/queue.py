"""兼容层:保留 task_queue 单例名,实际实现已迁到 scheduler.Scheduler。

api/tasks.py、api/ws.py、main.py 里的 `from .queue import task_queue`
继续可用,无需改动。
"""
from __future__ import annotations

from ..config import settings
from .scheduler import Scheduler

#: 全局调度器单例。worker 数取 config.yaml 的 worker.num_workers(默认 1)。
task_queue = Scheduler(num_workers=settings.worker.num_workers)

__all__ = ["Scheduler", "task_queue"]
