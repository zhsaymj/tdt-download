"""多 worker 下的任务分配与暂停路由。

阶段 2 的重点:worker 数从 1 放开后,验证派发与信号路由仍精确 ——
1 个 worker 时"发给错误的 worker"这个 bug 不可能暴露,多 worker 才会。
"""
import asyncio
import unittest
from unittest.mock import MagicMock

from backend.core.messages import ControlMessage
from backend.core.scheduler import Scheduler


class TestParallelRouting(unittest.TestCase):
    def _make(self, n):
        s = Scheduler(num_workers=n)
        s._workers = [MagicMock() for _ in range(n)]
        s._control_queues = [MagicMock() for _ in range(n)]
        s._signal_queues = [MagicMock() for _ in range(n)]
        s._idle = list(range(n))
        return s

    def test_two_tasks_go_to_two_workers(self):
        """两个任务应分派给两个不同 worker,而不是挤在同一个。"""
        s = self._make(2)
        asyncio.run(s.enqueue("t1"))
        asyncio.run(s.enqueue("t2"))

        self.assertEqual(s._task_worker, {"t1": 0, "t2": 1})
        self.assertEqual(s._idle, [])

    def test_third_task_waits(self):
        """第三个任务没有空闲 worker,应进入等待队列。"""
        s = self._make(2)
        asyncio.run(s.enqueue("t1"))
        asyncio.run(s.enqueue("t2"))
        asyncio.run(s.enqueue("t3"))

        self.assertEqual(s._pending, ["t3"])
        self.assertNotIn("t3", s._task_worker)

    def test_finished_frees_worker_and_dispatches_pending(self):
        """任务结束后 worker 变空闲,等待队列里的任务被派发。"""
        s = self._make(1)
        asyncio.run(s.enqueue("t1"))
        asyncio.run(s.enqueue("t2"))
        self.assertEqual(s._pending, ["t2"])

        asyncio.run(s._handle_event({"kind": "finished", "task_id": "t1"}))

        self.assertNotIn("t1", s._task_worker)
        self.assertIn("t2", s._task_worker)
        self.assertEqual(s._pending, [])

    def test_pause_targets_correct_worker_when_parallel(self):
        """两个任务并行时,暂停只发给对应的那个 worker 的**信号队列**。

        1 个 worker 时这个 bug 不可能暴露 —— 只有多 worker 才测得出路由错误。
        """
        s = self._make(2)
        asyncio.run(s.enqueue("t1"))
        asyncio.run(s.enqueue("t2"))

        s.request_pause("t2")

        s._signal_queues[1].put.assert_called_with(ControlMessage.pause("t2"))
        s._signal_queues[0].put.assert_not_called()
        s._control_queues[1].put.assert_called_once()   # 只有最初的 run

    def test_cancel_targets_correct_worker_when_parallel(self):
        """取消同样只发给目标任务所在 worker。"""
        s = self._make(2)
        asyncio.run(s.enqueue("t1"))
        asyncio.run(s.enqueue("t2"))

        s.request_cancel("t1")

        s._signal_queues[0].put.assert_called_with(ControlMessage.cancel("t1"))
        s._signal_queues[1].put.assert_not_called()

    def test_worker_slot_reuse_after_multiple_rounds(self):
        """多轮派发-结束循环后,槽位不应泄漏或错配。

        回归保护:若 finished 回收逻辑写错(如漏 append 或重复 append),
        _idle 会逐渐失真,几轮后任务再也派发不出去。
        """
        s = self._make(2)
        for i in range(6):
            tid = f"t{i}"
            asyncio.run(s.enqueue(tid))
        # 前两个在跑,其余 4 个排队
        self.assertEqual(sorted(s._task_worker.keys()), ["t0", "t1"])
        self.assertEqual(len(s._pending), 4)

        # 逐个结束,排队的应依次补位
        for i in range(6):
            asyncio.run(s._handle_event({"kind": "finished", "task_id": f"t{i}"}))

        self.assertEqual(s._task_worker, {})
        self.assertEqual(s._pending, [])
        self.assertEqual(sorted(s._idle), [0, 1], "两轮结束后槽位应完整归还")


if __name__ == "__main__":
    unittest.main()
