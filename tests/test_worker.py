"""测试 worker 进程入口与控制循环。"""
import unittest
from multiprocessing import Queue
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch, MagicMock

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.messages import ControlMessage, EventMessage


class TestWorkerBasics(unittest.TestCase):
    """测试 worker 基本启动与关闭。"""

    def test_worker_can_import(self):
        """worker 模块可以导入。"""
        from backend.core import worker
        self.assertIsNotNone(worker)

    def test_worker_has_entry_point(self):
        """worker 有 worker_main 入口函数。"""
        from backend.core.worker import worker_main
        self.assertTrue(callable(worker_main))

    def test_worker_shutdown_on_empty_queue(self):
        """worker 收到 shutdown 消息后退出循环。"""
        from backend.core.worker import worker_main

        control_q = Queue()
        event_q = Queue()

        # 发送 shutdown
        control_q.put(ControlMessage.shutdown())

        # 在主进程中直接调用（测试用，实际会在子进程）
        worker_main(control_q, event_q, worker_id="test-worker")

        # worker 应该已退出，队列应该为空
        self.assertTrue(control_q.empty())


class TestWorkerTaskExecution(unittest.TestCase):
    """测试 worker 任务执行逻辑。"""

    def test_nonexistent_task_sends_log_and_finished(self):
        """不存在的任务发送日志和 finished 事件。"""
        from backend.core.worker import worker_main

        control_q = Queue()
        event_q = Queue()

        # 发送不存在的任务
        control_q.put(ControlMessage.run("nonexistent-task-id"))
        control_q.put(ControlMessage.shutdown())

        worker_main(control_q, event_q, worker_id="test-worker")

        # 应该收到至少一个事件
        events = []
        while not event_q.empty():
            events.append(event_q.get())

        # 应该有 log 和 finished 消息
        kinds = [e["kind"] for e in events]
        self.assertIn("log", kinds)
        self.assertIn("finished", kinds)

        # finished 消息应该包含 task_id
        finished = [e for e in events if e["kind"] == "finished"][0]
        self.assertEqual(finished["task_id"], "nonexistent-task-id")

    @patch("backend.core.runner.run_task")
    @patch("backend.core.worker.get_task")
    def test_emit_forwards_to_event_queue(self, mock_get_task, mock_run_task):
        """emit 闭包能正确转发进度到 event_queue。"""
        from backend.core.worker import worker_main

        # Mock 任务存在
        mock_get_task.return_value = {"id": "test-task", "name": "测试"}

        # Mock run_task 调用 emit
        async def fake_run(task_id, emit):
            emit({"type": "progress", "id": task_id, "downloaded": 10})

        mock_run_task.side_effect = fake_run

        control_q = Queue()
        event_q = Queue()

        control_q.put(ControlMessage.run("test-task"))
        control_q.put(ControlMessage.shutdown())

        worker_main(control_q, event_q, worker_id="test-worker")

        # 检查事件队列
        events = []
        while not event_q.empty():
            events.append(event_q.get())

        # 应该有一个 event 消息包含我们的进度
        event_msgs = [e for e in events if e["kind"] == "event"]
        self.assertGreater(len(event_msgs), 0)

        payload = event_msgs[0]["payload"]
        self.assertEqual(payload["type"], "progress")
        self.assertEqual(payload["id"], "test-task")
        self.assertEqual(payload["downloaded"], 10)

    @patch("backend.core.runner.run_task")
    @patch("backend.core.worker.get_task")
    def test_task_sends_finished_event(self, mock_get_task, mock_run_task):
        """任务结束后发送 finished 事件。"""
        from backend.core.worker import worker_main

        mock_get_task.return_value = {"id": "test-task", "name": "测试"}

        async def fake_run(task_id, emit):
            pass  # 空任务

        mock_run_task.side_effect = fake_run

        control_q = Queue()
        event_q = Queue()

        control_q.put(ControlMessage.run("test-task"))
        control_q.put(ControlMessage.shutdown())

        worker_main(control_q, event_q, worker_id="test-worker")

        # 检查事件队列
        events = []
        while not event_q.empty():
            events.append(event_q.get())

        # 应该有 finished 消息
        finished_msgs = [e for e in events if e["kind"] == "finished"]
        self.assertEqual(len(finished_msgs), 1)
        self.assertEqual(finished_msgs[0]["task_id"], "test-task")


class TestWorkerLogging(unittest.TestCase):
    def test_worker_log_forwarded_to_event_queue(self):
        """worker 进程内写的日志会出现在 event_queue 里。"""
        import multiprocessing as mp
        from backend.core.worker import worker_main

        control_q = mp.Queue()
        event_q = mp.Queue()
        control_q.put(ControlMessage.run("nonexistent-task"))
        control_q.put(ControlMessage.shutdown())

        proc = mp.Process(target=worker_main, args=(control_q, event_q, "w0"))
        proc.start()
        proc.join(timeout=15)

        logs = []
        while not event_q.empty():
            m = event_q.get_nowait()
            if m.get("kind") == "log":
                logs.append(m)
        self.assertTrue(any("worker" in m["msg"] or "not found" in m["msg"]
                            for m in logs),
                        f"应收到 worker 日志,实际:{logs}")


if __name__ == "__main__":
    unittest.main()
