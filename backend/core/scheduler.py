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

from .logs import logger
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
        """启动 worker 子进程与事件消费协程(须在运行中的事件循环里调用)。

        幂等:lifespan 可能被跑两次(如 `with TestClient(app)`),重复 start 会
        再起 N 个 worker 而 _idle 里是重复的下标 —— 派发时下标与实际进程错位,
        任务投给错误的 worker(控制信号也会跟着投错)。
        """
        if self._workers:
            logger.warning("调度器已启动,忽略重复 start()")
            return
        for i in range(self._num_workers):
            cq: mp.Queue = mp.Queue()
            sq: mp.Queue = mp.Queue()
            # daemon=True:主进程退出时不必逐个收尾。⚠️ Windows 上 daemon 进程
            # **不允许**再创建 multiprocessing 子进程 —— 当前 worker 内部只用
            # ThreadPoolExecutor + subprocess(下载/PDAL/3dtiles),不冲突;
            # 但阶段 2 若在 worker 内引入 mp 池会立刻抛
            # "daemonic processes are not allowed to have children"。
            p = mp.Process(target=worker_main,
                           args=(cq, sq, self._event_queue, f"w{i}"),
                           daemon=True)
            p.start()
            self._control_queues.append(cq)
            self._signal_queues.append(sq)
            self._workers.append(p)
            self._idle.append(i)

        self._consumer_task = asyncio.create_task(self._event_consumer())

    async def shutdown(self, timeout: float = 10.0) -> None:
        """通知各 worker 退出,超时则强杀。

        是 async 而非同步:单个 worker 最多 join timeout 秒,多 worker 就是
        timeout×N。直接挂在 FastAPI lifespan 里(或 Ctrl-C 收尾)会**阻塞事件
        循环**数十秒,期间服务完全无响应。故真正的阻塞收尾放进线程执行。
        """
        self._closing = True

        # 先停消费协程,再关 worker:否则收尾期间 worker 退出会被 _reap_dead_workers
        # 判成崩溃,把在跑任务误落库为 failed(正常收尾不该报失败)。
        if self._consumer_task is not None:
            consumer, self._consumer_task = self._consumer_task, None
            consumer.cancel()
            try:
                # 必须 await:cancel() 只是发出请求,不等它收工就返回,残留的
                # 协程可能在本函数返回后仍往广播里塞事件。
                await consumer
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("事件消费协程收尾异常")

        await asyncio.to_thread(self._shutdown_workers, timeout)

        # 关闭后再 enqueue 的任务不会被派发,清空以免留下"看起来还在排队"的
        # 假象(配合 enqueue 的 _closing 守卫)。
        self._pending.clear()
        self._task_worker.clear()

    def _shutdown_workers(self, timeout: float) -> None:
        """阻塞式收尾:投 shutdown 消息 → join → 超时强杀(由 shutdown 放到线程里跑)。"""
        for q in self._control_queues:
            try:
                q.put(ControlMessage.shutdown())
            except Exception:
                pass

        for p in self._workers:
            try:
                p.join(timeout=timeout)
                if p.is_alive():
                    p.terminate()
                    p.join(timeout=2)
            except Exception:
                logger.exception("worker 进程收尾异常")

        self._workers.clear()
        self._control_queues.clear()
        self._signal_queues.clear()
        self._idle.clear()

    # ---------- 任务派发 ----------

    async def enqueue(self, task_id: str) -> None:
        """派发任务给空闲 worker;无空闲则排队等待。

        去重:同一任务已在等待队列或正在执行时不再入队。重复入队会让它被两个
        worker 并发执行,共写**同一 output_path 与瓦片缓存目录**(文件级损坏)。
        api 层的 `clear_control → update_task → enqueue` 三步非原子,前端双击
        按钮就能让两个请求都通过校验、都走到这里。
        """
        if self._closing:
            logger.warning("调度器已关闭,忽略任务[%s] 入队", task_id)
            return
        if task_id in self._task_worker:
            # 已在执行。注意 api 层「任务刚跑完、finished 事件还没被消费」的
            # 窗口里也会命中这里(消费协程 0.5s 轮询),那一次入队会被丢掉 ——
            # 记一笔,免得成为无日志的"点了开始却没跑"。
            logger.warning("任务[%s] 正在执行,忽略重复入队", task_id)
            return
        if task_id in self._pending:
            return
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

        依赖不变式:任务要么在 _pending、要么在 _task_worker,**不会同时在**
        (由 enqueue 去重保证)。否则这里的顺序判断会把运行中任务的暂停当成
        "还在排队"而只做移出队列 —— 正在跑的它收不到 pause,暂停静默失效。
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
        """兼容 api 层的空实现。

        旧 TaskQueue 里它清的是 pause/cancel 标志;而在新设计里那两个标志在
        **worker 侧**,由 worker 每次 run 开头自行清除(worker.py:81),故这里
        已无事可做。

        ⚠️ 这里**绝对不能**动 _task_worker —— 它是「运行中」标记,不是控制标志。
        删掉后迟到的 finished 就找不到 owner(_handle_event 里 `pop` 得 None),
        槽位永久丢失:num_workers=1 时后续任务全部静默堆在 _pending,无异常
        也无日志(已实测复现)。
        """
        return None

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
        """处理一条 worker 回传的事件。

        ⚠️ 字段取值一律用 .get + 判空,不要 fields[...]:parse_event 只校验
        kind,不校验各 kind 的必填字段(与 worker 侧 parse_control 同款约定)。
        裸取值抛出的 KeyError 会一路冒出到 _event_consumer,把消费协程**永久
        杀死** —— 此后所有进度/日志/广播全部停止,前端停在旧状态且无任何日志。
        """
        try:
            kind, fields = parse_event(msg)
        except ValueError:
            logger.warning("无法解析的 worker 事件,已忽略:%s", msg)
            return

        if kind == "finished":
            task_id = fields.get("task_id")
            if not task_id:
                logger.warning("finished 事件缺少 task_id,已忽略:%s", msg)
                return
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
            payload = fields.get("payload")
            if not isinstance(payload, dict):
                logger.warning("event 事件缺少 payload,已忽略:%s", msg)
                return
            await self._broadcast(payload)
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
                # 队列空 = 0.5s 一次的巡检时机:顺手回收崩溃 worker 的槽位。
                for task_id, reason in self._reap_dead_workers():
                    await self._fail_orphan(task_id, reason)
                continue
            except Exception:
                await asyncio.sleep(0.1)
                continue
            # 单条事件处理失败不得杀死消费协程:它一死,进度/日志/广播全部
            # 静默停止,前端永久停在旧状态,且没有任何可观测信号。
            try:
                await self._handle_event(msg)
            except Exception:
                logger.exception("处理 worker 事件失败:%s", msg)

    # ---------- worker 健康巡检 ----------

    def _reap_dead_workers(self) -> list[tuple[str, str]]:
        """巡检已退出的 worker:回收槽位,并返回其遗留任务 [(task_id, 原因)]。

        为什么必须有:worker 崩溃(原生 OOM / os._exit / 被信号杀死,见
        log_forwarder 的说明)后不会再投 finished,而 _task_worker / _idle 只在
        finished 里回收 —— 不主动巡检,调度器会永远认为该 worker 还忙,后续
        任务全部静默堆在 _pending,库里任务永远 running,前端永久转圈。

        保持同步、只回收调度状态:落库与广播要在事件循环里做,交给调用方
        (`_event_consumer` → `_fail_orphan`),这样单测也能直接调本函数验证。
        """
        orphans: list[tuple[str, str]] = []
        dead: set[int] = set()
        for i, p in enumerate(self._workers):
            try:
                # exitcode is None 表示"从未启动"而非"已退出":后者才要回收。
                if p.is_alive() or p.exitcode is None:
                    continue
            except Exception:
                continue
            dead.add(i)

        if not dead:
            return orphans

        for task_id, idx in list(self._task_worker.items()):
            if idx in dead:
                reason = (f"worker[{idx}] 已退出,任务中断")
                orphans.append((task_id, reason))
                logger.error("任务[%s] %s", task_id, reason)
        for task_id, _ in orphans:
            self._task_worker.pop(task_id, None)
        for i in dead:
            if i not in self._idle:
                self._idle.append(i)

        # 槽位已归还,趁机把等待队列推进(否则要等下一个 finished 才动)
        self._dispatch_pending()
        return orphans

    async def _fail_orphan(self, task_id: str, reason: str) -> None:
        """把因 worker 崩溃而失去归宿的任务落库为 failed(否则永远是 running)。"""
        from ..models import update_task
        try:
            update_task(task_id, status="failed", message=reason)
        except Exception:
            logger.exception("任务[%s] 落库 failed 状态时出错", task_id)
        # 广播给前端,别让它一直转圈
        await self._broadcast(
            {"type": "task", "id": task_id, "status": "failed",
             "message": reason})

    def _reemit_log(self, fields: dict) -> None:
        """worker 日志交给主进程 logger 输出(统一落盘 + 推 WS)。"""
        level = getattr(logging, str(fields.get("level", "INFO")).upper(),
                        logging.INFO)
        # 级别名可能撞上 logging 模块的非 int 属性(如 "BASIC_FORMAT" 是 str),
        # getattr 会拿到非 int 值 → logger.log 抛 TypeError。而坏消息来自子进程
        # 或外部输入,不该让主进程日志通路崩掉(它与消费协程同栈,后果见 I1)。
        if not isinstance(level, int):
            level = logging.INFO
        logger.log(level, fields.get("msg", ""))
