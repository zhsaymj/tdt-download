"""验证 worker 按 provider 分发到正确的 runner,以及运行期暂停生效。"""
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

# 添加项目根目录到 sys.path(与 test_worker.py 一致,供直接以脚本方式运行)
sys.path.insert(0, str(Path(__file__).parent.parent))

# 跨测试模块复用 logger 快照还原(与 test_worker.py 同一份实现,避免抄第二遍)。
# tests/ 无 __init__.py,靠命名空间包导入,故必须先补齐项目根的 sys.path。
from tests.test_worker import _LoggerSnapshotMixin


class TestWorkerDispatch(unittest.TestCase):
    """provider → runner 的分发守卫。

    local_osgb / local_pointcloud 没有瓦片行列号,误入 runner.py 栅格管线时
    点云单文件恰好能通过本地文件校验,后果隐蔽,故分发必须在此拦住。
    """

    def _dispatch(self, provider):
        from backend.core import worker
        with patch.object(worker, "get_task",
                          return_value={"id": "t1", "provider": provider}):
            return worker._resolve_runner("t1")

    def test_pointcloud_dispatches_to_runner_3d(self):
        with patch("backend.core.runner_3d.run_task") as mock3d:
            got = self._dispatch("local_pointcloud")
        self.assertIs(got, mock3d)

    def test_osgb_dispatches_to_runner_3d(self):
        with patch("backend.core.runner_3d.run_task") as mock3d:
            got = self._dispatch("local_osgb")
        self.assertIs(got, mock3d)

    def test_image_dispatches_to_runner(self):
        with patch("backend.core.runner.run_task") as mock2d:
            got = self._dispatch("tianditu_img")
        self.assertIs(got, mock2d)

    def test_unknown_task_raises(self):
        from backend.core import worker
        with patch.object(worker, "get_task", return_value=None):
            with self.assertRaises(LookupError):
                worker._resolve_runner("ghost")


class TestPauseDuringRun(_LoggerSnapshotMixin, unittest.TestCase):
    """回归保护:任务运行期间 pause 必须生效。

    若把信号接收和任务执行放回同一个线程,pause 消息会一直留在队列里
    直到任务自己跑完 —— 暂停功能静默失效(已实测复现)。

    用线程而非子进程跑 worker_main:patch 无法跨 spawn 子进程(子进程重新
    import 模块,父进程的补丁不生效),而这里要验证的"信号线程能否在任务
    阻塞时收到信号"本就是线程级问题,不需要真进程。子进程版端到端验证
    留给 Task 8。
    """

    def test_pause_takes_effect_while_task_running(self):
        import queue as queue_mod
        import threading
        import time
        from backend.core.messages import ControlMessage
        from backend.core import worker as worker_mod

        seen_stop = queue_mod.Queue()
        task_q, signal_q, event_q = (queue_mod.Queue() for _ in range(3))

        def fake_resolve(_task_id):
            # 返回 async 函数:与真实 runner 一致(_run_task 走 run_until_complete)
            return lambda tid, emit, should_stop: _long_task(
                tid, emit, should_stop, seen_stop)

        with patch.object(worker_mod, "get_task",
                          return_value={"id": "t1",
                                        "provider": "tianditu_img"}), \
             patch.object(worker_mod, "_resolve_runner", fake_resolve):
            t = threading.Thread(
                target=worker_mod.worker_main,
                args=(task_q, signal_q, event_q, "w0"),
                daemon=True,
            )
            t.start()
            try:
                task_q.put(ControlMessage.run("t1"))
                time.sleep(0.5)                    # 让任务跑起来
                signal_q.put(ControlMessage.pause("t1"))

                try:
                    stopped = seen_stop.get(timeout=8)
                except queue_mod.Empty:
                    stopped = None
                self.assertTrue(stopped, "任务未在收到 pause 后停止")
            finally:
                task_q.put(ControlMessage.shutdown())
                t.join(timeout=10)
                if t.is_alive():
                    self.fail("worker 未退出")


async def _long_task(task_id, emit, should_stop, seen_stop):
    """持续 5 秒检查 should_stop,一旦为真就回报并退出。"""
    import asyncio
    for _ in range(50):
        if should_stop():
            seen_stop.put(True)
            return
        await asyncio.sleep(0.1)
    seen_stop.put(False)


if __name__ == "__main__":
    unittest.main()
