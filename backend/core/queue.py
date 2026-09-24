"""兼容层:保留 task_queue 单例名,实际实现已迁到 scheduler.Scheduler。

api/tasks.py、api/ws.py、main.py 里的 `from .queue import task_queue`
继续可用,无需改动。
"""
from __future__ import annotations

from .scheduler import Scheduler

#: 阶段 1 固定单 worker。
#: TODO(Task 7):接入配置项。Config 目前**没有** num_workers 字段(config.yaml
#: 也没有该键),原先写的 getattr(settings, "num_workers", 1) 永远取到默认值 1,
#: 却读起来像已接配置 —— 用户按计划文档写上 num_workers: 2 也不会生效且无提示。
_NUM_WORKERS = 1

#: 全局调度器单例。
task_queue = Scheduler(num_workers=_NUM_WORKERS)

__all__ = ["Scheduler", "task_queue"]
