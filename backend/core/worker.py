"""Worker 进程入口与控制循环。

进程模型:主进程通过 control_queue 下发任务(run/shutdown)、通过 signal_queue
下发中断信号(pause/cancel),worker 执行后通过 event_queue 回传进度。

⚠️ 两条下发队列**必须分开**:接收 run 的主循环会阻塞在任务执行上
(``loop.run_until_complete``),若与信号接收同线程,暂停会完全失效;
若与信号接收共队列,两个消费线程会互相抢走对方的消息。
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
import traceback
from multiprocessing import Queue

from .messages import EventMessage, parse_control
from ..models import get_task


def worker_main(control_queue: Queue, signal_queue: Queue,
                event_queue: Queue, worker_id: str) -> None:
    """Worker 进程入口函数。

    Args:
        control_queue: 任务控制队列(run / shutdown),由任务线程消费
        signal_queue:  中断信号队列(pause / cancel),由信号线程消费
        event_queue:   事件上报队列
        worker_id:     worker 标识
    """
    _setup_worker_env(event_queue, worker_id)

    # 本地控制状态镜像:task_id -> 'pause' | 'cancel'
    # 由 _SignalConsumer 线程独占写入,任务线程只读;CPython 下 dict 的单键
    # 读/写是原子的,无需额外加锁。
    control_state: dict[str, str] = {}
    signals = _SignalConsumer(signal_queue, control_state, worker_id)
    signals.start()

    # 子进程需要创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        while True:
            msg = control_queue.get()          # 只会有 run / shutdown
            try:
                kind, fields = parse_control(msg)
            except ValueError as e:
                from .logs import logger
                logger.warning("worker[%s] 控制消息非法:%s", worker_id, e)
                continue
            if kind == "shutdown":
                break
            if kind == "run":
                task_id = fields["task_id"]
                # 清除之前的控制状态(如果是恢复运行)
                control_state.pop(task_id, None)
                _run_task(task_id, event_queue, control_state, loop, worker_id)
    finally:
        signals.stop()
        loop.close()


def _resolve_runner(task_id: str):
    """查任务并按 provider 返回对应 runner(分发即守卫)。

    local_osgb / local_pointcloud 没有瓦片行列号,必须走 runner_3d;不经分发
    会误入栅格管线(点云单文件恰好能通过 runner.py 的本地文件校验,后果更隐蔽)。
    建筑白模由 runner.run_task 内部自行转 runner_buildings,这里不单独分支。
    规则与主进程 TaskQueue._resolve_runner 保持一致。
    """
    task = get_task(task_id)
    if not task:
        raise LookupError(f"任务不存在:{task_id}")

    provider = task.get("provider") or ""
    if provider in ("local_osgb", "local_pointcloud"):
        from .runner_3d import run_task as run_3d
        return run_3d
    from .runner import run_task as run_2d
    return run_2d


def _setup_worker_env(event_queue: Queue, worker_id: str) -> None:
    """worker 启动环境:日志改为转发到主进程。

    必须在导入业务模块之前/之初调用:core.logs 导入时会给 tdt logger 挂
    文件与环形 handler,子进程再挂一份会与主进程争抢同一个日志文件。

    为何是这个顺序(先 import logger 再 install):logs._setup() 由 _tdt_ready
    守卫,只在 logs 模块首次导入时执行。若反过来先 install,后续任一业务模块
    首次 ``from .logs import logger`` 会触发 _setup() 把 RotatingFileHandler
    重新挂上,转发静默失效且无任何报错。install_forwarding 内部现已自行 import
    .logs 兜底,此处仍显式按序书写以免后人重排。
    """
    from .log_forwarder import install_forwarding
    from .logs import logger

    install_forwarding(logger, event_queue, worker_id)
    logger.info("worker[%s] 已启动,pid=%s", worker_id, os.getpid())


class _SignalConsumer(threading.Thread):
    """后台线程:只消费 pause / cancel,更新 control_state。

    必须与任务执行分离线程 —— 任务阻塞在 run_until_complete 时,同线程
    收不到任何信号,暂停会完全失效(已实测)。

    也必须与 run/shutdown 分队列 —— 两个消费线程共读一条队列会互相抢走
    对方的消息。
    """

    def __init__(self, signal_queue: Queue, control_state: dict[str, str],
                 worker_id: str):
        super().__init__(daemon=True, name=f"signal-{worker_id}")
        self._q = signal_queue
        self._state = control_state
        self._worker_id = worker_id
        # 注意:不要命名为 _stop —— threading.Thread 自带同名内部方法,
        # 实例属性会把它盖掉,join() 内部调 self._stop() 时抛
        # TypeError: 'Event' object is not callable。
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """置停止位并等线程退出。

        带 join 是为了让 worker 退出时线程已被回收(否则残留的守护线程可能
        在调用方还原日志 handler 之后才写一笔)。
        """
        self._stop_event.set()
        self.join(timeout=1.0)

    def run(self) -> None:
        from .logs import logger
        while not self._stop_event.is_set():
            try:
                msg = self._q.get(timeout=0.5)
            except Exception:
                continue                       # 超时轮询,便于 stop() 生效
            try:
                kind, fields = parse_control(msg)
            except ValueError as e:
                logger.warning("worker[%s] 信号解析失败:%s", self._worker_id, e)
                continue
            if kind in ("pause", "cancel"):
                self._state[fields["task_id"]] = kind


def _run_task(task_id: str, event_queue: Queue,
              control_state: dict[str, str],
              loop: asyncio.AbstractEventLoop,
              worker_id: str = "") -> None:
    """执行单个任务:构造回调、跑事件循环、异常兜底、上报 finished。

    Args:
        task_id: 任务 ID
        event_queue: 事件队列
        control_state: 控制状态镜像(由信号线程写入,这里只读)
        loop: 事件循环
        worker_id: worker 标识符(直发日志时带上,便于多 worker 区分来源)

    注意:本函数会阻塞直到任务结束(``run_until_complete``),故调用它的
    主循环在任务运行期间读不到 control_queue —— 控制信号必须走 signal_queue。
    """
    def emit(payload: dict) -> None:
        event_queue.put(EventMessage.event(payload))

    def should_stop() -> str | None:
        """返回原始控制值(不是 bool):_handle_stop 靠它区分 pause/cancel。"""
        return control_state.get(task_id)

    try:
        runner = _resolve_runner(task_id)
        loop.run_until_complete(runner(task_id, emit, should_stop))

    except LookupError as e:
        # 任务不存在(直发队列,显式带 worker_id)
        event_queue.put(EventMessage.log("warning", str(e), time.time(),
                                         worker_id))

    except Exception as e:
        # 任务执行异常:记录日志,发送失败事件(直发队列,显式带 worker_id)
        error_msg = f"Task {task_id} failed: {e}\n{traceback.format_exc()}"
        event_queue.put(EventMessage.log("error", error_msg, time.time(),
                                         worker_id))
        event_queue.put(EventMessage.event({
            "type": "task", "id": task_id,
            "status": "failed", "message": str(e)
        }))

    finally:
        # 任务结束(无论成功/失败/取消)
        event_queue.put(EventMessage.finished(task_id))
        control_state.pop(task_id, None)
