"""进程内异步任务队列 + 进度广播。

单机自用:一个后台 worker 顺序处理任务队列,任务内部再做瓦片级并发。
进度通过 broadcast 回调推送给 WebSocket 订阅者。
"""
from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

# 进度订阅者:接收 dict 消息的异步回调
Subscriber = Callable[[dict], Awaitable[None]]


class TaskQueue:
    def __init__(self):
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers: set[Subscriber] = set()
        self._worker: asyncio.Task | None = None
        self._runner: Callable[[str, Callable[[dict], None]], Awaitable[None]] | None = None
        # 运行中任务的协作控制:task_id -> 'pause' | 'cancel'
        self._control: dict[str, str] = {}

    def set_runner(self, runner):
        """注册任务执行函数:async runner(task_id, emit)。"""
        self._runner = runner

    def start(self):
        if self._worker is None:
            self._worker = asyncio.create_task(self._loop())

    async def enqueue(self, task_id: str):
        await self._queue.put(task_id)

    # ---------- 任务控制(暂停 / 取消) ----------

    def request_pause(self, task_id: str):
        self._control[task_id] = "pause"

    def request_cancel(self, task_id: str):
        self._control[task_id] = "cancel"

    def control_of(self, task_id: str) -> str | None:
        return self._control.get(task_id)

    def clear_control(self, task_id: str):
        self._control.pop(task_id, None)

    # ---------- 订阅 ----------

    def subscribe(self, cb: Subscriber):
        self._subscribers.add(cb)

    def unsubscribe(self, cb: Subscriber):
        self._subscribers.discard(cb)

    async def _broadcast(self, msg: dict):
        # 复制一份,避免迭代中集合被修改
        for cb in list(self._subscribers):
            try:
                await cb(msg)
            except Exception:
                self._subscribers.discard(cb)

    async def broadcast(self, msg: dict):
        """对外广播一条消息(如 tk 池切换/用尽提示)。"""
        await self._broadcast(msg)

    # ---------- worker ----------

    async def _loop(self):
        while True:
            task_id = await self._queue.get()
            try:
                if self._runner is None:
                    continue
                # 出队时若已被取消(删除),跳过不执行
                if self._control.get(task_id) == "cancel":
                    continue
                # 同步 emit:把进度包装成广播,交给事件循环
                loop = asyncio.get_running_loop()

                def emit(msg: dict, _loop=loop):
                    asyncio.run_coroutine_threadsafe(self._broadcast(msg), _loop)

                await self._runner(task_id, emit)
            except Exception as e:  # 单个任务失败不拖垮 worker
                try:
                    from .logs import logger
                    logger.exception("任务[%s] 执行异常:%s", task_id, e)
                except Exception:
                    pass
                await self._broadcast(
                    {"type": "task", "id": task_id, "status": "failed", "message": str(e)}
                )
            finally:
                self._control.pop(task_id, None)
                self._queue.task_done()


# 全局队列单例
task_queue = TaskQueue()
