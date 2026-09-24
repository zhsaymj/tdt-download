"""Worker 进程入口与控制循环。

进程模型:主进程通过 control_queue 下发任务,worker 执行后通过 event_queue 回传进度。
"""
from __future__ import annotations

import asyncio
import os
import time
import traceback
from multiprocessing import Queue
from typing import Callable

from .messages import ControlMessage, EventMessage, parse_control
from ..models import get_task


def worker_main(control_queue: Queue, event_queue: Queue, worker_id: str) -> None:
    """Worker 进程入口函数。

    Args:
        control_queue: 接收控制消息的队列
        event_queue: 发送事件消息的队列
        worker_id: worker 标识符
    """
    _setup_worker_env(event_queue, worker_id)

    # 子进程需要创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        _control_loop(control_queue, event_queue, worker_id, loop)
    finally:
        loop.close()


def _setup_worker_env(event_queue: Queue, worker_id: str) -> None:
    """worker 启动环境:日志改为转发到主进程。

    必须在导入业务模块之前/之初调用:core.logs 导入时会给 tdt logger 挂
    文件与环形 handler,子进程再挂一份会与主进程争抢同一个日志文件。
    """
    from .log_forwarder import install_forwarding
    from .logs import logger

    install_forwarding(logger, event_queue)
    logger.info("worker[%s] 已启动,pid=%s", worker_id, os.getpid())


def _control_loop(control_queue: Queue, event_queue: Queue,
                  worker_id: str, loop: asyncio.AbstractEventLoop) -> None:
    """主控制循环:顺序消费控制消息。

    Args:
        control_queue: 控制消息队列
        event_queue: 事件消息队列
        worker_id: worker 标识符
        loop: 事件循环
    """
    # 本地控制状态镜像:task_id -> 'pause' | 'cancel'
    # runner 通过闭包读取此状态决定是否停止
    control_state: dict[str, str] = {}

    while True:
        try:
            msg = control_queue.get()
            kind, fields = parse_control(msg)

            if kind == "shutdown":
                break

            elif kind == "run":
                task_id = fields["task_id"]
                # 清除之前的控制状态(如果是恢复运行)
                control_state.pop(task_id, None)
                _run_task(task_id, event_queue, control_state, loop)

            elif kind == "pause":
                task_id = fields["task_id"]
                control_state[task_id] = "pause"

            elif kind == "cancel":
                task_id = fields["task_id"]
                control_state[task_id] = "cancel"

        except Exception as e:
            # 消息解析或处理异常不应崩溃 worker
            error_msg = f"Worker error: {e}\n{traceback.format_exc()}"
            event_queue.put(EventMessage.log("error", error_msg, time.time()))


def _run_task(task_id: str, event_queue: Queue,
              control_state: dict[str, str],
              loop: asyncio.AbstractEventLoop) -> None:
    """执行单个任务。

    Args:
        task_id: 任务 ID
        event_queue: 事件队列
        control_state: 控制状态镜像
        loop: 事件循环

    注意：当前 control_state 与 should_stop 闭包已创建但未实际使用。
    现有 runner.py 直接调用 task_queue.control_of() 访问主进程单例。
    Task 4 将改造 run_task 签名，接受 should_stop 参数，届时 worker
    进程将通过此镜像实现真正的进程隔离控制。
    """
    # 查询任务
    task = get_task(task_id)
    if task is None:
        # 不存在的任务:记录日志但不崩溃
        event_queue.put(EventMessage.log(
            "warning", f"Task {task_id} not found in database", time.time()
        ))
        event_queue.put(EventMessage.finished(task_id))
        return

    # 创建 emit 闭包:转发进度到 event_queue
    def emit(payload: dict) -> None:
        event_queue.put(EventMessage.event(payload))

    # 创建 control_checker 闭包:供 runner 检查是否需要停止
    # TODO(Task 4): 将此闭包传递给 run_task，替代其内部的 task_queue.control_of() 调用
    # 届时签名将变为: run_task(task_id, emit, should_stop)
    def should_stop() -> bool:
        return control_state.get(task_id) in ("pause", "cancel")

    try:
        # 导入 runner (惰性导入避免循环依赖)
        from .runner import run_task

        # 在事件循环中运行异步任务
        # TODO(Task 4): 传递 should_stop 参数
        loop.run_until_complete(run_task(task_id, emit))

    except Exception as e:
        # 任务执行异常:记录日志,发送失败事件
        error_msg = f"Task {task_id} failed: {e}\n{traceback.format_exc()}"
        event_queue.put(EventMessage.log("error", error_msg, time.time()))
        event_queue.put(EventMessage.event({
            "type": "task", "id": task_id,
            "status": "failed", "message": str(e)
        }))

    finally:
        # 任务结束(无论成功/失败/取消)
        event_queue.put(EventMessage.finished(task_id))
        control_state.pop(task_id, None)
