"""Worker 进程入口与控制循环。

进程模型:主进程通过 control_queue 下发任务(run/shutdown)、通过 signal_queue
下发中断信号(pause/cancel),worker 执行后通过 event_queue 回传进度。

⚠️ 两条下发队列**必须分开**:接收 run 的主循环会阻塞在任务执行上
(``loop.run_until_complete``),若与信号接收同线程,暂停会完全失效;
若与信号接收共队列,两个消费线程会互相抢走对方的消息。

⚠️ 坏消息不得杀死任何线程:parse_control 只校验 kind,不校验各 kind 的必填
字段,取值(``fields["task_id"]``)必须与解析收在同一个 try 里。
"""
from __future__ import annotations

import asyncio
import os
import queue
import threading
import time
import traceback
from multiprocessing import Queue

# 模块级导入 logger:作为 logs._setup() 的首次触发点,确保它早于
# install_forwarding 发生(理由见 _setup_worker_env;模块导入先于任何函数调用,
# 比写在函数体里更不容易被后人重排)。
from .logs import logger
from .messages import EventMessage, parse_control
from ..models import get_task


class TaskNotFoundError(Exception):
    """任务在数据库中不存在。

    刻意不用 LookupError:它是 KeyError/IndexError 的基类,而 runner 里到处是
    ``task["bbox"]`` 这类取值,用宽泛类型兜「任务不存在」会把任务执行期的
    真实异常误判成任务不存在 —— 那条分支既不落库也不投 failed 事件,任务会
    永远停在 running,前端显示「运行中」,成为不推进、不失败、不解除的幽灵。
    """


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
                # 取值与解析同 try:parse_control 只校验 kind,缺 task_id 的
                # run 消息会在下一行抛 KeyError,冒泡出去就是 worker 进程死亡
                # (没有恢复路径:Task 6 不做 is_alive 健康检查,往死进程的
                # Queue put 也会成功,任务于是一直卡着)。
                task_id = fields["task_id"] if kind == "run" else None
            except (ValueError, KeyError) as e:
                logger.warning("worker[%s] 控制消息非法,已忽略:%s", worker_id, e)
                continue

            if kind == "shutdown":
                break
            if kind == "run":
                # 清除之前的控制状态(如果是恢复运行)
                control_state.pop(task_id, None)
                _run_task(task_id, event_queue, control_state, loop, worker_id)
    finally:
        # 清理阶段不应再抛:异常会顶替真正的异常冒出去,且后面的清理会被跳过
        # (事件循环不 close 会连默认线程池 executor 一起泄漏)。
        try:
            signals.stop()
        except Exception as e:
            logger.warning("worker[%s] 信号线程回收失败:%s", worker_id, e)
        try:
            loop.close()
        except Exception as e:
            logger.warning("worker[%s] 事件循环关闭失败:%s", worker_id, e)
        # 置空当前事件循环:本函数会被同进程多次调用(测试),留着已 close 的
        # loop 会让后续 asyncio.get_event_loop() 拿到一个死 loop,直到真正用它
        # 才抛 "Event loop is closed"(报错点离根因很远);置空后当场就明确报
        # "no current event loop"。全仓已无 get_event_loop() 消费方。
        asyncio.set_event_loop(None)


def _resolve_runner(task_id: str):
    """查任务并按 provider 返回对应 runner(分发即守卫)。

    local_osgb / local_pointcloud 没有瓦片行列号,必须走 runner_3d;不经分发
    会误入栅格管线(点云单文件恰好能通过 runner.py 的本地文件校验,后果更隐蔽)。
    建筑白模由 runner.run_task 内部自行转 runner_buildings,这里不单独分支。
    判定规则收敛在 formats.is_3d_provider,避免各处分头硬编码 provider 元组而漂移
    (漂移后果是静默错路由)。
    """
    from .formats import is_3d_provider

    task = get_task(task_id)
    if not task:
        raise TaskNotFoundError(f"任务不存在:{task_id}")

    provider = task.get("provider") or ""
    if is_3d_provider(provider):
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
    .logs 兜底;本模块顶部的 ``from .logs import logger`` 已把该顺序固化在
    模块导入期,此处显式按序书写只是让意图在近处可见。
    """
    from .log_forwarder import install_forwarding

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

        join 前判 is_alive:start() 自身抛错(如无法新建线程)时线程从未启动,
        join 会抛 RuntimeError: cannot join thread before it is started。
        本函数在 worker_main 的 finally 里调用,该 RuntimeError 会顶替真正的
        异常冒出去(同文件的 runner_3d.py 里 watcher.join 也是这么守的)。
        """
        self._stop_event.set()
        if self.is_alive():
            self.join(timeout=1.0)

    def run(self) -> None:
        """线程主体:任何异常都不允许杀死本线程。

        守护线程死掉没有外部可观测信号 —— 信号线程一死,该 worker 的暂停/取消
        就永久失效且连日志都没有,正是最难发现的那种故障,故整体再兜一层。
        """
        while not self._stop_event.is_set():
            try:
                if not self._consume_once():
                    return                     # 队列已关闭,线程收工
            except Exception as e:
                logger.exception("worker[%s] 信号处理异常,已忽略:%s",
                                 self._worker_id, e)

    def _consume_once(self) -> bool:
        """消费一条信号;返回 False 表示队列已关闭(线程应结束)。"""
        try:
            msg = self._q.get(timeout=0.5)
        except queue.Empty:
            return True                        # 超时轮询,便于 stop() 生效
        except (EOFError, OSError, ValueError):
            # 队列已关闭(主进程退出)。必须精准捕获:对已 close 的 mp.Queue
            # 调 get 是**立刻**抛异常而非阻塞,宽泛的 except + continue 会变成
            # 100% CPU 忙等。
            return False

        try:
            kind, fields = parse_control(msg)
            # 取值与解析同 try:缺 task_id 的 pause 会抛 KeyError,把信号线程
            # 带走(该 worker 从此暂停/取消永久失效且无日志)。
            task_id = fields["task_id"] if kind in ("pause", "cancel") else None
        except (ValueError, KeyError) as e:
            logger.warning("worker[%s] 信号非法,已忽略:%s", self._worker_id, e)
            return True

        if task_id is not None:
            self._state[task_id] = kind
        else:
            # run / shutdown 不属于本队列(见模块 docstring 的队列分工),
            # 出现即接错队列,记一笔免得静默丢失。
            logger.warning("worker[%s] 信号队列收到非信号消息:%s,已忽略",
                           self._worker_id, kind)
        return True


def _run_task(task_id: str, event_queue: Queue,
              control_state: dict[str, str],
              loop: asyncio.AbstractEventLoop,
              worker_id: str) -> None:
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

    except TaskNotFoundError as e:
        # 任务不存在(直发队列,显式带 worker_id)。
        # 只兜专用异常:这里不投 failed 事件,主进程靠 worker 存活性判定,
        # 若用宽泛的 LookupError 会把 runner 里的 KeyError/IndexError 一并吞掉。
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
