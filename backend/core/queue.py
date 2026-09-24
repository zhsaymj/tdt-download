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
        # runner 签名:async runner(task_id, emit, should_stop)
        self._runner: Callable[..., Awaitable[None]] | None = None
        # 运行中任务的协作控制:task_id -> 'pause' | 'cancel'
        self._control: dict[str, str] = {}

    def set_runner(self, runner):
        """注册任务执行函数:async runner(task_id, emit, should_stop)。"""
        self._runner = runner

    def _resolve_runner(self, task_id: str):
        """按任务 provider 分发执行管线(分发即守卫)。

        local_osgb / local_pointcloud 是本地三维数据(osgb 目录 / las 文件),
        没有瓦片行列号,必须走 runner_3d;若不经分发会误入 runner.py 栅格
        管线(点云单文件恰好能通过 runner.py 的本地文件校验,后果更隐蔽)。
        惰性 import:避免 queue 与 models/runner_3d 形成模块级循环依赖。
        """
        from ..models import get_task

        task = get_task(task_id)
        if task and task.get("provider") in ("local_osgb", "local_pointcloud"):
            from .runner_3d import run_task as run_3d
            return run_3d
        return self._runner

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
                runner = self._resolve_runner(task_id)
                if runner is None:
                    continue
                # 出队时若已被取消(删除),跳过不执行
                if self._control.get(task_id) == "cancel":
                    continue
                # 同步 emit:把进度包装成广播,交给事件循环
                loop = asyncio.get_running_loop()

                def emit(msg: dict, _loop=loop):
                    asyncio.run_coroutine_threadsafe(self._broadcast(msg), _loop)

                # 临时兼容(Task 6/7 会随 TaskQueue→Scheduler 替换一并移除):
                # runner 已改为要求注入 should_stop 闭包(进程隔离后子进程里的
                # task_queue 是另一份副本,读不到主进程控制标志)。主进程内队列
                # 就地执行时,把本实例的控制状态包成闭包传进去即可。
                # 闭包回传 "pause"/"cancel" 原因而不只是 bool,runner 的
                # _handle_stop 才能把两者落成不同状态(取消→canceled)。
                def should_stop(_id=task_id):
                    return self._control.get(_id)

                await runner(task_id, emit, should_stop)
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
