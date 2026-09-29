# 后端计算与 Web 服务进程隔离 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 GDAL 长 GIL 占用操作移出 uvicorn 主进程,消除合并 tif / 切片时前端接口无响应的问题

**Architecture:** 常驻 worker 进程池消费任务队列,主进程只做调度与广播;token 计数用租约协议避免各进程独立计数超配额;资源准入保护磁盘/内存底线

**Tech Stack:** multiprocessing.Process + Queue, psutil, SQLite WAL, 现有 FastAPI + asyncio

**实施分阶段:**
- 阶段 1: 进程隔离(worker 数固定为 1,不做并行),验收标准是产出与改造前逐字节一致
- 阶段 2: 并行(worker 数可配) + token 租约
- 阶段 3: 资源准入 + psutil
- 阶段 4: 打包验证

---

## 阶段 1: 进程隔离(单 worker)

### Task 1: 跨进程协议消息定义

**Files:**
- Create: `backend/core/messages.py`
- Test: `tests/test_messages.py`

- [ ] **Step 1: 写消息编解码的失败测试**

```python
# tests/test_messages.py
import unittest
from backend.core.messages import ControlMessage, EventMessage, parse_control, parse_event


class TestMessages(unittest.TestCase):
    def test_control_run_encode(self):
        msg = ControlMessage.run("task-123")
        self.assertEqual(msg, {"kind": "run", "task_id": "task-123"})

    def test_control_pause_encode(self):
        msg = ControlMessage.pause("task-456")
        self.assertEqual(msg, {"kind": "pause", "task_id": "task-456"})

    def test_control_cancel_encode(self):
        msg = ControlMessage.cancel("task-789")
        self.assertEqual(msg, {"kind": "cancel", "task_id": "task-789"})

    def test_control_shutdown_encode(self):
        msg = ControlMessage.shutdown()
        self.assertEqual(msg, {"kind": "shutdown"})

    def test_event_progress_encode(self):
        msg = EventMessage.event({"type": "progress", "downloaded": 100})
        self.assertEqual(msg, {
            "kind": "event",
            "payload": {"type": "progress", "downloaded": 100}
        })

    def test_event_log_encode(self):
        msg = EventMessage.log("INFO", "test message", 1234567890.0)
        self.assertEqual(msg, {
            "kind": "log",
            "level": "INFO",
            "msg": "test message",
            "ts": 1234567890.0
        })

    def test_event_finished_encode(self):
        msg = EventMessage.finished("task-abc")
        self.assertEqual(msg, {"kind": "finished", "task_id": "task-abc"})

    def test_parse_control_run(self):
        result = parse_control({"kind": "run", "task_id": "t1"})
        self.assertEqual(result, ("run", {"task_id": "t1"}))

    def test_parse_control_unknown_kind(self):
        with self.assertRaises(ValueError):
            parse_control({"kind": "unknown"})

    def test_parse_event_event(self):
        result = parse_event({"kind": "event", "payload": {"type": "task"}})
        self.assertEqual(result, ("event", {"payload": {"type": "task"}}))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_messages -v`
Expected: ModuleNotFoundError: No module named 'backend.core.messages'

- [ ] **Step 3: 实现消息编解码**

```python
# backend/core/messages.py
"""跨进程协议消息定义:主进程 ↔ worker 进程。

Control 消息(主 → worker):通过 control_queue 投递。
Event 消息(worker → 主):通过 event_queue 回传。
"""
from __future__ import annotations


class ControlMessage:
    """主进程向 worker 下达的控制指令。"""

    @staticmethod
    def run(task_id: str) -> dict:
        """执行任务。"""
        return {"kind": "run", "task_id": task_id}

    @staticmethod
    def pause(task_id: str) -> dict:
        """暂停任务(协作式)。"""
        return {"kind": "pause", "task_id": task_id}

    @staticmethod
    def cancel(task_id: str) -> dict:
        """取消任务(协作式)。"""
        return {"kind": "cancel", "task_id": task_id}

    @staticmethod
    def shutdown() -> dict:
        """通知 worker 退出。"""
        return {"kind": "shutdown"}


class EventMessage:
    """worker 向主进程上报的事件。"""

    @staticmethod
    def event(payload: dict) -> dict:
        """进度/状态事件(原 emit 的消息体)。"""
        return {"kind": "event", "payload": payload}

    @staticmethod
    def log(level: str, msg: str, ts: float) -> dict:
        """日志转发。"""
        return {"kind": "log", "level": level, "msg": msg, "ts": ts}

    @staticmethod
    def finished(task_id: str) -> dict:
        """任务结束(正常 / 异常 / 被取消)。"""
        return {"kind": "finished", "task_id": task_id}


def parse_control(msg: dict) -> tuple[str, dict]:
    """解析控制消息,返回 (kind, 剩余字段)。"""
    kind = msg.get("kind")
    if kind not in ("run", "pause", "cancel", "shutdown"):
        raise ValueError(f"Unknown control kind: {kind}")
    return kind, {k: v for k, v in msg.items() if k != "kind"}


def parse_event(msg: dict) -> tuple[str, dict]:
    """解析事件消息,返回 (kind, 剩余字段)。"""
    kind = msg.get("kind")
    if kind not in ("event", "log", "finished"):
        raise ValueError(f"Unknown event kind: {kind}")
    return kind, {k: v for k, v in msg.items() if k != "kind"}
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_messages -v`
Expected: 所有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/core/messages.py tests/test_messages.py
git commit -m "feat(queue): 新增跨进程协议消息定义"
```

---

### Task 2: Worker 进程入口与控制循环

**Files:**
- Create: `backend/core/worker.py`
- Test: `tests/test_worker.py`

- [ ] **Step 1: 写 worker 启动与关闭的失败测试**

```python
# tests/test_worker.py
import unittest
import multiprocessing as mp
import time
from backend.core.worker import worker_main
from backend.core.messages import ControlMessage


class TestWorker(unittest.TestCase):
    def test_worker_shutdown_on_empty_command(self):
        """worker 收到 shutdown 指令后退出。"""
        control_q = mp.Queue()
        event_q = mp.Queue()
        control_q.put(ControlMessage.shutdown())

        proc = mp.Process(target=worker_main, args=(control_q, event_q, 0))
        proc.start()
        proc.join(timeout=2)

        self.assertFalse(proc.is_alive(), "worker 应已退出")
        self.assertEqual(proc.exitcode, 0)

    def test_worker_ignores_unknown_task(self):
        """worker 遇到不存在的 task_id 不崩溃。"""
        control_q = mp.Queue()
        event_q = mp.Queue()
        control_q.put(ControlMessage.run("nonexistent-task"))
        control_q.put(ControlMessage.shutdown())

        proc = mp.Process(target=worker_main, args=(control_q, event_q, 0))
        proc.start()
        proc.join(timeout=3)

        self.assertFalse(proc.is_alive())
        # 应有一条 finished 事件
        events = []
        while not event_q.empty():
            events.append(event_q.get_nowait())
        finished = [e for e in events if e.get("kind") == "finished"]
        self.assertEqual(len(finished), 1)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_worker -v`
Expected: ModuleNotFoundError: No module named 'backend.core.worker'

- [ ] **Step 3: 实现 worker 入口**

```python
# backend/core/worker.py
"""Worker 进程入口:从控制队列取任务,执行后回传事件。

单个 worker 顺序消费任务;多个 worker 并发消费。
"""
from __future__ import annotations

import multiprocessing as mp
import sys
import traceback

from .messages import ControlMessage, EventMessage, parse_control


def worker_main(control_queue: mp.Queue, event_queue: mp.Queue, worker_id: int):
    """Worker 进程主循环。

    Args:
        control_queue: 接收主进程下发的控制消息
        event_queue: 向主进程回传事件(进度 / 日志 / 完成)
        worker_id: 标识此 worker(仅用于日志)
    """
    try:
        _setup_worker_env(worker_id, event_queue)
        _control_loop(control_queue, event_queue, worker_id)
    except KeyboardInterrupt:
        pass
    except Exception:
        exc = traceback.format_exc()
        try:
            event_queue.put(EventMessage.log("ERROR", f"Worker {worker_id} crashed: {exc}", 0))
        except Exception:
            pass
        sys.exit(1)


def _setup_worker_env(worker_id: int, event_queue: mp.Queue):
    """初始化 worker 环境:日志转发 + 信号屏蔽(未来)。"""
    # TODO Task 3: 配置日志 handler 转发到 event_queue
    pass


def _control_loop(control_queue: mp.Queue, event_queue: mp.Queue, worker_id: int):
    """主循环:取控制消息 → 执行 → 回传事件。"""
    # worker 本地的暂停/取消状态镜像(从控制消息同步)
    control_state: dict[str, str] = {}

    while True:
        msg = control_queue.get()
        kind, fields = parse_control(msg)

        if kind == "shutdown":
            break

        if kind == "run":
            task_id = fields["task_id"]
            _run_task(task_id, event_queue, control_state)
            event_queue.put(EventMessage.finished(task_id))

        elif kind == "pause":
            task_id = fields["task_id"]
            control_state[task_id] = "pause"

        elif kind == "cancel":
            task_id = fields["task_id"]
            control_state[task_id] = "cancel"


def _run_task(task_id: str, event_queue: mp.Queue, control_state: dict[str, str]):
    """执行单个任务。"""
    from ..models import get_task

    task = get_task(task_id)
    if not task:
        event_queue.put(EventMessage.log("WARNING", f"Task {task_id} not found", 0))
        return

    # 分发到对应管线
    provider = task.get("provider", "")
    if provider in ("local_osgb", "local_pointcloud"):
        from .runner_3d import run_task_sync
        runner = run_task_sync
    elif provider == "local_buildings_osm":
        from .runner_buildings import run_task_sync
        runner = run_task_sync
    else:
        from .runner import run_task_sync
        runner = run_task_sync

    # emit 闭包:包装 EventMessage.event 并投递到事件队列
    def emit(payload: dict):
        event_queue.put(EventMessage.event(payload))

    # should_stop 闭包:读 worker 本地镜像
    def should_stop() -> str | None:
        return control_state.get(task_id)

    try:
        runner(task_id, emit, should_stop)
    except Exception as e:
        exc = traceback.format_exc()
        event_queue.put(EventMessage.log("ERROR", f"Task {task_id} failed: {exc}", 0))
        emit({"type": "task", "id": task_id, "status": "failed", "message": str(e)})
    finally:
        control_state.pop(task_id, None)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_worker -v`
Expected: 所有测试 PASS(第二个测试 nonexistent-task 分支会触发 log WARNING)

- [ ] **Step 5: 提交**

```bash
git add backend/core/worker.py tests/test_worker.py
git commit -m "feat(queue): 新增 worker 进程入口与控制循环"
```

---

### Task 3: 日志转发到主进程

**Files:**
- Modify: `backend/core/worker.py:_setup_worker_env`
- Create: `backend/core/log_forwarder.py`
- Test: `tests/test_log_forwarder.py`

- [ ] **Step 1: 写日志转发的失败测试**

```python
# tests/test_log_forwarder.py
import unittest
import multiprocessing as mp
import logging
import time
from backend.core.log_forwarder import QueueLogHandler


class TestLogForwarder(unittest.TestCase):
    def test_handler_forwards_to_queue(self):
        """日志记录被转发到队列。"""
        q = mp.Queue()
        handler = QueueLogHandler(q)
        logger = logging.getLogger("test_forward")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)

        logger.info("test message")
        time.sleep(0.1)

        self.assertFalse(q.empty())
        msg = q.get_nowait()
        self.assertEqual(msg["kind"], "log")
        self.assertEqual(msg["level"], "INFO")
        self.assertIn("test message", msg["msg"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_log_forwarder -v`
Expected: ModuleNotFoundError: No module named 'backend.core.log_forwarder'

- [ ] **Step 3: 实现日志转发 handler**

```python
# backend/core/log_forwarder.py
"""日志转发 handler:worker 进程的日志转发到主进程事件队列。"""
from __future__ import annotations

import logging
import multiprocessing as mp
import time

from .messages import EventMessage


class QueueLogHandler(logging.Handler):
    """把 LogRecord 转成 EventMessage.log 投递到队列。"""

    def __init__(self, event_queue: mp.Queue):
        super().__init__()
        self.event_queue = event_queue

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            ts = time.time()
            self.event_queue.put(EventMessage.log(record.levelname, msg, ts))
        except Exception:
            self.handleError(record)
```

- [ ] **Step 4: 在 worker_main 中安装日志 handler**

```python
# backend/core/worker.py (修改 _setup_worker_env 函数)
def _setup_worker_env(worker_id: int, event_queue: mp.Queue):
    """初始化 worker 环境:日志转发 + 信号屏蔽(未来)。"""
    from .log_forwarder import QueueLogHandler
    from .logs import logger

    handler = QueueLogHandler(event_queue)
    handler.setFormatter(logging.Formatter("[Worker %(process)d] %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
```

需要在文件顶部添加 import:

```python
import logging
```

- [ ] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_log_forwarder -v`
Expected: 所有测试 PASS

- [ ] **Step 6: 提交**

```bash
git add backend/core/log_forwarder.py backend/core/worker.py tests/test_log_forwarder.py
git commit -m "feat(queue): worker 日志转发到主进程"
```

---

### Task 4: runner*.py 改造为同步接口

**Files:**
- Modify: `backend/core/runner.py` (新增 `run_task_sync`)
- Modify: `backend/core/runner_3d.py` (新增 `run_task_sync`)
- Modify: `backend/core/runner_buildings.py` (新增 `run_task_sync`)
- Test: `tests/test_runner_sync.py`

- [ ] **Step 1: 写同步 runner 的失败测试**

```python
# tests/test_runner_sync.py
import unittest
from unittest.mock import MagicMock
from backend.core.runner import run_task_sync


class TestRunnerSync(unittest.TestCase):
    def test_run_task_sync_exists(self):
        """run_task_sync 函数存在且签名正确。"""
        self.assertTrue(callable(run_task_sync))
        # 签名: (task_id, emit, should_stop)
        import inspect
        sig = inspect.signature(run_task_sync)
        params = list(sig.parameters.keys())
        self.assertEqual(params, ["task_id", "emit", "should_stop"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_runner_sync -v`
Expected: AttributeError: module 'backend.core.runner' has no attribute 'run_task_sync'

- [ ] **Step 3: 在 runner.py 新增同步版本**

在 `backend/core/runner.py` 文件末尾添加:

```python
def run_task_sync(task_id: str, emit: Callable[[dict], None], should_stop: Callable[[], str | None]):
    """同步版本:供 worker 进程调用。

    Args:
        task_id: 任务 ID
        emit: 进度回调(同步)
        should_stop: 协作式停止检查,返回 'pause' / 'cancel' / None
    """
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # 把同步 emit 包装成异步
        async def async_emit(msg: dict):
            emit(msg)

        # 把 should_stop 注入到任务上下文(runner 内部已有的协作逻辑)
        # 原 run_task 从 task_queue.control_of(task_id) 读取,现在改为从参数传入
        loop.run_until_complete(_run_task_impl(task_id, async_emit, should_stop))
    finally:
        loop.close()


async def _run_task_impl(task_id: str, emit: Callable[[dict], Awaitable[None]], should_stop: Callable[[], str | None]):
    """原 run_task 的实现,抽出成独立函数以便同步/异步复用。

    这里把原 run_task 函数体中所有 task_queue.control_of(task_id) 的调用
    改为 should_stop(),其余逻辑保持不变。
    """
    # 将原 run_task 函数体复制到这里,并做两处修改:
    # 1. 所有 task_queue.control_of(task_id) 改为 should_stop()
    # 2. 所有 task_queue.clear_control(task_id) 删除(worker 自己管理 control_state)
    # 详见下一步
```

- [ ] **Step 4: 重构 run_task 复用 _run_task_impl**

修改 `backend/core/runner.py` 中原有的 `run_task` 函数:

```python
async def run_task(task_id: str, emit: Callable[[dict], None]):
    """异步版本:供主进程原地调用(兼容旧逻辑,未来会移除)。"""
    async def async_emit(msg: dict):
        emit(msg)

    def should_stop_from_queue() -> str | None:
        from .queue import task_queue
        return task_queue.control_of(task_id)

    await _run_task_impl(task_id, async_emit, should_stop_from_queue)
```

然后把原 `run_task` 函数体中的主逻辑移到 `_run_task_impl`,并替换所有:
- `task_queue.control_of(task_id)` → `should_stop()`
- 删除所有 `task_queue.clear_control(task_id)` 调用

(实际改动较大,需要读取完整 runner.py 后操作,这里仅示意结构)

- [ ] **Step 5: runner_3d.py 和 runner_buildings.py 同样改造**

在两个文件末尾各添加:

```python
def run_task_sync(task_id: str, emit: Callable[[dict], None], should_stop: Callable[[], str | None]):
    """同步版本:供 worker 进程调用。"""
    import asyncio
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        async def async_emit(msg: dict):
            emit(msg)
        loop.run_until_complete(_run_task_impl(task_id, async_emit, should_stop))
    finally:
        loop.close()
```

同样把原 `run_task` 函数体抽成 `_run_task_impl`,替换 `control_of` 调用为 `should_stop()`

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_runner_sync -v`
Expected: 所有测试 PASS

- [ ] **Step 7: 提交**

```bash
git add backend/core/runner.py backend/core/runner_3d.py backend/core/runner_buildings.py tests/test_runner_sync.py
git commit -m "refactor(runner): 抽出 run_task_sync 供 worker 进程调用"
```

---

### Task 5: Scheduler 进程池管理

**Files:**
- Create: `backend/core/scheduler.py`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: 写 scheduler 启动与关闭的失败测试**

```python
# tests/test_scheduler.py
import unittest
import time
from backend.core.scheduler import Scheduler


class TestScheduler(unittest.TestCase):
    def test_scheduler_start_stop(self):
        """scheduler 能正常启动和关闭。"""
        scheduler = Scheduler(num_workers=1)
        scheduler.start()
        time.sleep(0.5)
        self.assertTrue(scheduler.is_alive())

        scheduler.stop()
        time.sleep(1)
        self.assertFalse(scheduler.is_alive())

    def test_scheduler_submit_task(self):
        """scheduler 能接收任务并派发到 worker。"""
        scheduler = Scheduler(num_workers=1)
        scheduler.start()

        # 提交一个不存在的任务(会触发 worker 的 log WARNING)
        scheduler.submit("test-task-999")
        time.sleep(2)

        scheduler.stop()
        # 无异常即通过


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scheduler -v`
Expected: ModuleNotFoundError: No module named 'backend.core.scheduler'

- [ ] **Step 3: 实现 Scheduler**

```python
# backend/core/scheduler.py
"""进程池调度器:管理 worker 进程 + 双向消息队列。"""
from __future__ import annotations

import multiprocessing as mp
import threading
import time

from .messages import ControlMessage, EventMessage, parse_event
from .worker import worker_main


class Scheduler:
    """常驻 worker 进程池 + 事件消费线程。"""

    def __init__(self, num_workers: int = 1):
        self.num_workers = num_workers
        self.control_queue = mp.Queue()
        self.event_queue = mp.Queue()
        self.workers: list[mp.Process] = []
        self.event_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        # 事件回调:由外部注册(如 TaskQueue.broadcast)
        self.on_event: callable[[dict], None] | None = None

    def start(self):
        """启动 worker 进程池与事件消费线程。"""
        for i in range(self.num_workers):
            p = mp.Process(target=worker_main, args=(self.control_queue, self.event_queue, i), daemon=True)
            p.start()
            self.workers.append(p)

        self.event_thread = threading.Thread(target=self._event_loop, daemon=True)
        self.event_thread.start()

    def stop(self, timeout: float = 5.0):
        """优雅关闭:发 shutdown 指令,等待 worker 退出。"""
        for _ in self.workers:
            self.control_queue.put(ControlMessage.shutdown())

        deadline = time.time() + timeout
        for p in self.workers:
            remaining = max(0, deadline - time.time())
            p.join(timeout=remaining)
            if p.is_alive():
                p.terminate()
                p.join(timeout=1)

        self._stop_event.set()
        if self.event_thread and self.event_thread.is_alive():
            self.event_thread.join(timeout=2)

    def is_alive(self) -> bool:
        """至少一个 worker 还活着。"""
        return any(p.is_alive() for p in self.workers)

    def submit(self, task_id: str):
        """提交任务到 worker 池。"""
        self.control_queue.put(ControlMessage.run(task_id))

    def pause(self, task_id: str):
        """暂停任务(协作式)。"""
        self.control_queue.put(ControlMessage.pause(task_id))

    def cancel(self, task_id: str):
        """取消任务(协作式)。"""
        self.control_queue.put(ControlMessage.cancel(task_id))

    def _event_loop(self):
        """事件消费线程:从 event_queue 取消息,转发给 on_event 回调。"""
        while not self._stop_event.is_set():
            try:
                msg = self.event_queue.get(timeout=0.5)
                kind, fields = parse_event(msg)

                if kind == "event":
                    payload = fields["payload"]
                    if self.on_event:
                        self.on_event(payload)

                elif kind == "log":
                    # TODO: 转发到主进程日志系统
                    level = fields["level"]
                    log_msg = fields["msg"]
                    print(f"[{level}] {log_msg}")

                elif kind == "finished":
                    task_id = fields["task_id"]
                    # worker 已完成,清理主进程的 control 状态(如果有)
                    pass

            except Exception:
                if self._stop_event.is_set():
                    break
                time.sleep(0.1)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_scheduler -v`
Expected: 所有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/core/scheduler.py tests/test_scheduler.py
git commit -m "feat(queue): 新增 Scheduler 进程池调度器"
```

---

### Task 6: 集成 Scheduler 到 TaskQueue

**Files:**
- Modify: `backend/core/queue.py`
- Test: `tests/test_queue_integration.py`

- [ ] **Step 1: 写集成测试**

```python
# tests/test_queue_integration.py
import unittest
import asyncio
import time
from backend.core.queue import TaskQueue


class TestQueueIntegration(unittest.TestCase):
    def test_queue_uses_scheduler(self):
        """TaskQueue 使用 Scheduler 派发任务。"""
        queue = TaskQueue(use_scheduler=True, num_workers=1)
        queue.start()

        # 注册进度回调
        events = []
        async def collect(msg: dict):
            events.append(msg)

        queue.subscribe(collect)

        # 提交任务(不存在的 task,会触发 worker log)
        asyncio.run(queue.enqueue("test-999"))
        time.sleep(2)

        queue.stop()
        # 无异常即通过(events 可能为空,因为任务不存在不会有进度)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_queue_integration -v`
Expected: TypeError: TaskQueue.__init__() got unexpected keyword argument 'use_scheduler'

- [ ] **Step 3: 修改 TaskQueue 集成 Scheduler**

在 `backend/core/queue.py` 中修改 `TaskQueue.__init__`:

```python
class TaskQueue:
    def __init__(self, use_scheduler: bool = False, num_workers: int = 1):
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers: set[Subscriber] = set()
        self._worker: asyncio.Task | None = None
        self._runner: Callable[[str, Callable[[dict], None]], Awaitable[None]] | None = None
        self._control: dict[str, str] = {}

        # 新增:进程池调度器
        self.use_scheduler = use_scheduler
        self.scheduler: Scheduler | None = None
        if use_scheduler:
            from .scheduler import Scheduler
            self.scheduler = Scheduler(num_workers=num_workers)
            self.scheduler.on_event = self._on_scheduler_event
```

修改 `start` 方法:

```python
    def start(self):
        if self.use_scheduler and self.scheduler:
            self.scheduler.start()
        if self._worker is None:
            self._worker = asyncio.create_task(self._loop())
```

新增 `stop` 方法和 `_on_scheduler_event`:

```python
    def stop(self):
        """停止队列(含 scheduler)。"""
        if self.scheduler:
            self.scheduler.stop()

    def _on_scheduler_event(self, payload: dict):
        """Scheduler 的事件回调:转成异步广播。"""
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(self._broadcast(payload), loop)
```

修改 `_loop` 方法,若启用 scheduler 则派发到进程池:

```python
    async def _loop(self):
        while True:
            task_id = await self._queue.get()
            try:
                # 若启用 scheduler,派发到进程池
                if self.use_scheduler and self.scheduler:
                    if self._control.get(task_id) == "cancel":
                        continue
                    self.scheduler.submit(task_id)
                    continue

                # 否则走原地执行逻辑
                runner = self._resolve_runner(task_id)
                if runner is None:
                    continue
                if self._control.get(task_id) == "cancel":
                    continue

                loop = asyncio.get_running_loop()
                def emit(msg: dict, _loop=loop):
                    asyncio.run_coroutine_threadsafe(self._broadcast(msg), _loop)

                await runner(task_id, emit)
            except Exception as e:
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
```

修改 `request_pause` 和 `request_cancel`,若启用 scheduler 则同步到进程池:

```python
    def request_pause(self, task_id: str):
        self._control[task_id] = "pause"
        if self.scheduler:
            self.scheduler.pause(task_id)

    def request_cancel(self, task_id: str):
        self._control[task_id] = "cancel"
        if self.scheduler:
            self.scheduler.cancel(task_id)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_queue_integration -v`
Expected: 所有测试 PASS

- [ ] **Step 5: 提交**

```bash
git add backend/core/queue.py tests/test_queue_integration.py
git commit -m "feat(queue): 集成 Scheduler 到 TaskQueue"
```

---

### Task 7: 配置项与初始化

**Files:**
- Modify: `backend/config.py`
- Modify: `backend/main.py`

- [ ] **Step 1: 在 config.py 新增 WorkerConfig**

在 `backend/config.py` 中添加:

```python
@dataclass
class WorkerConfig:
    """Worker 进程池配置。"""
    enabled: bool = True        # 是否启用进程池(False 则原地执行,用于对比验证)
    num_workers: int = 1        # worker 进程数(阶段 1 固定为 1)
    # 未来扩展:token_lease_size, resource_check_interval 等
```

在 `Config` 类中添加字段:

```python
@dataclass
class Config:
    # ... 现有字段 ...
    worker: WorkerConfig = field(default_factory=WorkerConfig)
```

在 `_load` 函数中添加加载逻辑:

```python
def _load(path: Path) -> Config:
    # ... 现有逻辑 ...
    worker_dict = data.get("worker", {})
    worker = WorkerConfig(
        enabled=worker_dict.get("enabled", True),
        num_workers=worker_dict.get("num_workers", 1),
    )
    return Config(
        # ... 现有字段 ...
        worker=worker,
    )
```

- [ ] **Step 2: 在 main.py 初始化 TaskQueue 时传参**

修改 `backend/main.py` 的启动部分(假设在 `lifespan` 或 `@app.on_event("startup")` 中):

```python
@app.on_event("startup")
async def startup():
    from .core.queue import task_queue
    from .config import settings

    # 注册 runner(原地执行时仍需要)
    from .core.runner import run_task
    task_queue.set_runner(run_task)

    # 根据配置启用进程池
    if settings.worker.enabled:
        task_queue.use_scheduler = True
        from .scheduler import Scheduler
        task_queue.scheduler = Scheduler(num_workers=settings.worker.num_workers)
        task_queue.scheduler.on_event = task_queue._on_scheduler_event

    task_queue.start()


@app.on_event("shutdown")
async def shutdown():
    from .core.queue import task_queue
    task_queue.stop()
```

- [ ] **Step 3: 在 config.example.yaml 添加示例**

在 `config.example.yaml` 末尾添加:

```yaml
# Worker 进程池配置
worker:
  enabled: true       # false = 原地执行(用于对比验证)
  num_workers: 1      # 阶段 1 固定为 1
```

- [ ] **Step 4: 提交**

```bash
git add backend/config.py backend/main.py config.example.yaml
git commit -m "feat(config): 新增 worker 进程池配置项"
```

---

### Task 8: 阶段 1 端到端验证

**Files:**
- Create: `tests/test_e2e_stage1.py`

- [ ] **Step 1: 写端到端测试(进程池 vs 原地执行产物一致性)**

```python
# tests/test_e2e_stage1.py
"""阶段 1 端到端验证:进程池与原地执行的产出逐字节一致。

前提:tests/fixtures/test-task-stage1.json 中预定义一个小任务
(如 2x2 瓦片,1 级,无裁剪),产出 GeoTIFF 约 1MB。
"""
import unittest
import shutil
import filecmp
from pathlib import Path
from backend.core.queue import TaskQueue
from backend.config import settings


class TestE2EStage1(unittest.TestCase):
    def test_process_isolation_output_identical(self):
        """进程池模式与原地执行模式产出逐字节一致。"""
        task_id = "test-stage1-isolation"
        output_dir = Path(settings.abs_path("./output")) / task_id

        # 清理旧产物
        if output_dir.exists():
            shutil.rmtree(output_dir)

        # 1. 原地执行
        queue_inline = TaskQueue(use_scheduler=False)
        from backend.core.runner import run_task
        queue_inline.set_runner(run_task)
        queue_inline.start()

        # 提交任务并等待完成
        import asyncio
        asyncio.run(queue_inline.enqueue(task_id))
        asyncio.run(queue_inline._queue.join())

        output_inline = output_dir / f"{task_id}.tif"
        self.assertTrue(output_inline.exists())
        inline_bytes = output_inline.read_bytes()

        # 备份产物
        backup = output_dir.parent / f"{task_id}_inline.tif"
        shutil.copy(output_inline, backup)

        # 清理
        shutil.rmtree(output_dir)

        # 2. 进程池执行
        queue_pool = TaskQueue(use_scheduler=True, num_workers=1)
        queue_pool.set_runner(run_task)
        queue_pool.start()

        asyncio.run(queue_pool.enqueue(task_id))
        import time
        time.sleep(10)  # 等待完成(真实测试中应监听事件)

        output_pool = output_dir / f"{task_id}.tif"
        self.assertTrue(output_pool.exists())
        pool_bytes = output_pool.read_bytes()

        # 3. 逐字节对比
        self.assertEqual(len(inline_bytes), len(pool_bytes))
        self.assertEqual(inline_bytes, pool_bytes)

        queue_pool.stop()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 准备测试 fixture**

创建 `tests/fixtures/test-task-stage1.json`(需要手动创建任务或用 SQL 插入):

```json
{
  "id": "test-stage1-isolation",
  "name": "阶段1验证任务",
  "provider": "tianditu_img",
  "bbox": "[116.3, 39.9, 116.4, 40.0]",
  "z_min": 10,
  "z_max": 10,
  "export": "geotiff",
  "status": "pending"
}
```

- [ ] **Step 3: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_e2e_stage1 -v`
Expected: 测试 PASS,两个 GeoTIFF 逐字节一致

- [ ] **Step 4: 提交**

```bash
git add tests/test_e2e_stage1.py tests/fixtures/
git commit -m "test(stage1): 端到端验证进程池与原地执行产出一致"
```

---

### Task 9: SQLite 并发写配置

**Files:**
- Modify: `backend/db.py`

- [ ] **Step 1: 在 get_conn 中补充 busy_timeout**

在 `backend/db.py` 的 `get_conn` 函数中修改:

```python
def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")  # 新增:等待 5s
    return conn
```

- [ ] **Step 2: 提交**

```bash
git add backend/db.py
git commit -m "fix(db): 补充 busy_timeout 应对多进程并发写"
```

---

### Task 10: PyInstaller freeze_support

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: 在 main.py 顶层添加 freeze_support**

在 `backend/main.py` 最顶部(所有 import 之后,app 定义之前)添加:

```python
import multiprocessing as mp

# PyInstaller 打包后子进程启动必须
if __name__ == "__main__":
    mp.freeze_support()
```

- [ ] **Step 2: 提交**

```bash
git add backend/main.py
git commit -m "fix(worker): 添加 freeze_support 支持打包后子进程"
```

---

## 阶段 1 验收

运行完整测试套件:

```bash
.venv/Scripts/python.exe -m unittest discover tests -v
```

预期:
- 所有新增测试(messages / worker / scheduler / queue_integration / e2e_stage1)通过
- 原有 160+ 测试不受影响

手动验收:
1. 启动后端(`start.bat`)
2. 前端创建一个小任务(如 2 级 2x2 瓦片)
3. 观察任务执行过程中,前端操作(切换任务列表、点击其他接口)是否流畅无卡顿
4. 对比 `worker.enabled=false` 与 `worker.enabled=true` 两种模式的产出 GeoTIFF,确认逐字节一致

---

## 阶段 2~4 占位

(占位,待阶段 1 验收通过后展开)

### 阶段 2: 并行与 token 租约
- Task 11: TokenPool 租约协议
- Task 12: 配置 num_workers > 1
- Task 13: 并行验证(两个任务同时运行)

### 阶段 3: 资源准入
- Task 14: psutil 磁盘/内存检查
- Task 15: 准入阈值配置
- Task 16: 准入拒绝提示前端

### 阶段 4: 打包验证
- Task 17: PyInstaller 打包
- Task 18: 打包后子进程启动测试
- Task 19: 打包后端到端验证

---

## 回归测试清单

每个阶段完成后必须通过:

1. **单元测试**: `.venv/Scripts/python.exe -m unittest discover tests -v`
2. **手动功能测试**:
   - 创建任务 → 下载 → 拼接 → 导出 GeoTIFF
   - 创建任务 → 下载 → 拼接 → 导出 TMS
   - 创建任务 → 暂停 → 继续 → 完成
   - 创建任务 → 取消 → 删除
   - 三维数据任务(OSGB / 点云)
   - 建筑白模任务
3. **前端交互测试**: 任务运行时,前端操作无卡顿
4. **产出一致性**: 新旧模式产出逐字节对比

---

## 注意事项

1. **阶段 1 是基础**:必须确保产出一致性,否则不进入阶段 2
2. **渐进式改动**:每个 task 独立可测,失败时易回退
3. **保留原地执行路径**:config 中 `worker.enabled=false` 可回退到改造前状态,用于对比调试
4. **日志完整性**:worker 日志必须能在主进程看到,便于排查问题
5. **协作式停止**:暂停/取消都是协作式,不能强杀进程(会丢进度)
