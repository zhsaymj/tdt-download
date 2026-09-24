"""任务调度器:管理工作进程池,把任务派发给空闲 worker。

与旧 TaskQueue 的差别:任务不在主进程执行,而是投给子进程;
进度/日志经 event_queue 回传,由 _event_consumer 协程转成广播。

暂停/取消的准确性依赖两点:
1. **每 worker 独立队列** —— 共享队列下暂停消息可能被别的 worker 收走;
2. **任务队列与信号队列分开** —— worker 侧由两个线程分别消费,
   共用一个队列会互相抢走对方的消息(任务线程把 pause 抢走当未知类型忽略)。
"""
from __future__ import annotations

import asyncio
import functools
import logging
import multiprocessing as mp
import queue
from typing import Awaitable, Callable

from .messages import ControlMessage, parse_event
from .worker import worker_main

Subscriber = Callable[[dict], Awaitable[None]]

#: 事件队列轮询超时(秒)。见 _event_consumer 的说明:必须是有限值。
_EVENT_POLL_SEC = 0.5


class Scheduler:
    """常驻 worker 进程池 + 任务派发 + 事件广播。"""

    def __init__(self, num_workers: int = 1):
        self._num_workers = max(1, int(num_workers))
        self._workers: list[mp.Process] = []
        # 每个 worker 两条队列:任务队列(run/shutdown)与信号队列(pause/cancel)。
        # 必须分开 —— 两条队列各由一个线程消费,共用一个会互相抢消息。
        self._control_queues: list[mp.Queue] = []
        self._signal_queues: list[mp.Queue] = []
        self._event_queue: mp.Queue = mp.Queue()
        self._idle: list[int] = []          # 空闲 worker 下标
        self._pending: list[str] = []       # 无空闲 worker 时的等待队列
        self._task_worker: dict[str, int] = {}   # task_id -> worker 下标
        self._subscribers: set[Subscriber] = set()
        self._consumer_task: asyncio.Task | None = None
        self._closing = False

    # ---------- 生命周期 ----------

    def start(self) -> None:
        """启动 worker 子进程与事件消费协程(须在运行中的事件循环里调用)。"""
        for i in range(self._num_workers):
            cq: mp.Queue = mp.Queue()
            sq: mp.Queue = mp.Queue()
            p = mp.Process(target=worker_main,
                           args=(cq, sq, self._event_queue, f"w{i}"),
                           daemon=True)
            p.start()
            self._control_queues.append(cq)
            self._signal_queues.append(sq)
            self._workers.append(p)
            self._idle.append(i)

        self._consumer_task = asyncio.create_task(self._event_consumer())

    def shutdown(self, timeout: float = 10.0) -> None:
        """通知各 worker 退出,超时则强杀。"""
        self._closing = True
        # 取消事件消费协程:让它别再去提交新的阻塞 get(单次 get 最长
        # _EVENT_POLL_SEC 秒后返回,故取消后线程能干净收工,见 _event_consumer)。
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            self._consumer_task = None
        for q in self._control_queues:
            try:
                q.put(ControlMessage.shutdown())
            except Exception:
                pass

        for p in self._workers:
            p.join(timeout=timeout)
            if p.is_alive():
                p.terminate()
                p.join(timeout=2)

        self._workers.clear()
        self._control_queues.clear()
        self._signal_queues.clear()
        self._idle.clear()

    # ---------- 任务派发 ----------

    async def enqueue(self, task_id: str) -> None:
        """派发任务给空闲 worker;无空闲则排队等待。"""
        self._pending.append(task_id)
        self._dispatch_pending()

    def _dispatch_pending(self) -> None:
        """把等待队列里的任务尽可能派发出去。"""
        while self._pending and self._idle:
            task_id = self._pending.pop(0)
            idx = self._idle.pop(0)
            self._task_worker[task_id] = idx
            try:
                self._control_queues[idx].put(ControlMessage.run(task_id))
            except Exception:
                # 队列不可用(worker 已死):回退状态,任务留在等待队列
                self._task_worker.pop(task_id, None)
                self._idle.append(idx)
                self._pending.insert(0, task_id)
                break

    # ---------- 任务控制 ----------

    def _send_signal(self, task_id: str, msg: dict) -> None:
        """把中断信号投给跑该任务的 worker 的**信号队列**;任务不在跑则忽略。

        走 signal_queue 而非 control_queue:后者由任务线程消费,而任务正在
        运行时就阻塞在 run_until_complete 上、根本不会去 get() —— pause/cancel
        投进去会一直躺着直到任务自己跑完(已实测)。
        """
        idx = self._task_worker.get(task_id)
        if idx is None or idx >= len(self._signal_queues):
            return
        try:
            self._signal_queues[idx].put(msg)
        except Exception:
            pass

    def request_pause(self, task_id: str) -> None:
        """请求暂停。

        两种情况都要处理:
        1. 任务已在跑 → 把消息投给跑它的那个 worker;
        2. 任务还在等待队列(未派发)→ 直接移出队列。否则空闲 worker 一出现
           就会把它跑起来,与 api/tasks.py:842 落库的 paused 状态矛盾。
        """
        if task_id in self._pending:
            self._pending.remove(task_id)
            return
        self._send_signal(task_id, ControlMessage.pause(task_id))

    def request_cancel(self, task_id: str) -> None:
        """请求取消。语义同 request_pause(删除流程依赖它拦住未派发的任务)。"""
        if task_id in self._pending:
            self._pending.remove(task_id)
            return
        self._send_signal(task_id, ControlMessage.cancel(task_id))

    def clear_control(self, task_id: str) -> None:
        """清本地记录。worker 侧的镜像会在下次 run 时自动清除。"""
        self._task_worker.pop(task_id, None)

    # ---------- 订阅与广播 ----------

    def subscribe(self, cb: Subscriber) -> None:
        self._subscribers.add(cb)

    def unsubscribe(self, cb: Subscriber) -> None:
        self._subscribers.discard(cb)

    async def _broadcast(self, msg: dict) -> None:
        for cb in list(self._subscribers):
            try:
                await cb(msg)
            except Exception:
                self._subscribers.discard(cb)

    async def broadcast(self, msg: dict) -> None:
        """对外广播(如 tk 池切换提示),与任务事件走同一条通路。"""
        await self._broadcast(msg)

    # ---------- 事件消费 ----------

    async def _handle_event(self, msg: dict) -> None:
        """处理一条 worker 回传的事件。"""
        try:
            kind, fields = parse_event(msg)
        except ValueError:
            return

        if kind == "finished":
            task_id = fields["task_id"]
            idx = self._task_worker.pop(task_id, None)
            if idx is not None and idx not in self._idle:
                self._idle.append(idx)
            self._dispatch_pending()
            # ⚠️ 不要把 "finished" 播给前端:frontendvue/src/stores/task.js:83
            # 是 `t.status = msg.status` 直接赋值,而终态集合为
            # ['done','failed','canceled'](:87)。"finished" 不在其中,卡片会
            # 显示界面不认识的状态、total_eta_sec 也不清零。
            # 真正的终态(running/done/failed/paused/canceled)已由 runner 经
            # emit 上报,这里只需处理调度状态。
        elif kind == "event":
            await self._broadcast(fields["payload"])
        elif kind == "log":
            self._reemit_log(fields)

    async def _event_consumer(self) -> None:
        """把 worker 回传的事件转成广播。

        mp.Queue.get() 是阻塞调用,必须放到线程里,否则卡住事件循环 ——
        那就又变回改造前「计算阻塞接口」的老问题。

        ⚠️ 必须带超时轮询,不能用无限期阻塞的 get:
        ``run_in_executor(None, ...)`` 用的是事件循环默认 ThreadPoolExecutor 的
        **非守护**线程,而 ``asyncio.run`` 收尾时会 ``shutdown_default_executor()``
        逐个 join 它们。无限期的 get 会让该线程永不返回 → join 永久卡住 →
        进程退不掉(实测:main 跑完进程仍挂着,uvicorn 的 Ctrl-C 也无效,只能
        强杀;换成普通 queue.Queue 也能复现,与 mp 无关)。带超时后该线程每
        0.5s 必然返回一次,取消消费协程即可让它顺利收工。
        """
        loop = asyncio.get_running_loop()
        while not self._closing:
            try:
                # block=True + timeout:超时抛 queue.Empty,而非永久阻塞
                msg = await loop.run_in_executor(
                    None,
                    functools.partial(self._event_queue.get, True,
                                      _EVENT_POLL_SEC))
            except queue.Empty:
                continue          # 队列空,回头检查 _closing
            except Exception:
                await asyncio.sleep(0.1)
                continue
            await self._handle_event(msg)

    def _reemit_log(self, fields: dict) -> None:
        """worker 日志交给主进程 logger 输出(统一落盘 + 推 WS)。"""
        from .logs import logger
        level = getattr(logging, str(fields.get("level", "INFO")).upper(),
                        logging.INFO)
        logger.log(level, fields.get("msg", ""))
