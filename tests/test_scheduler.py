"""Scheduler 单元测试:不启动真实子进程,验证消息构造与事件桥接。

注意:每个 worker 有**两条**队列 ——
control_queues 收 run/shutdown(任务线程读),
signal_queues  收 pause/cancel(信号线程读)。
两条分开是必须的:若共用一个队列,两个消费线程会互相抢走消息。
"""
import asyncio
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from backend.core.messages import ControlMessage
from backend.core.scheduler import Scheduler


class TestSchedulerMessages(unittest.TestCase):
    def _make(self, n=1):
        s = Scheduler(num_workers=n)
        s._workers = [MagicMock() for _ in range(n)]
        s._control_queues = [MagicMock() for _ in range(n)]
        s._signal_queues = [MagicMock() for _ in range(n)]
        s._idle = list(range(n))
        return s

    def test_enqueue_sends_run_to_idle_worker(self):
        s = self._make(1)
        asyncio.run(s.enqueue("t1"))

        s._control_queues[0].put.assert_called_once()
        self.assertEqual(s._control_queues[0].put.call_args[0][0],
                         ControlMessage.run("t1"))
        s._signal_queues[0].put.assert_not_called()
        self.assertEqual(s._idle, [], "派发后该 worker 应标记为忙")

    def test_enqueue_buffers_when_all_busy(self):
        s = self._make(1)
        s._idle = []
        asyncio.run(s.enqueue("t1"))

        s._control_queues[0].put.assert_not_called()
        self.assertEqual(s._pending, ["t1"], "应进入等待队列")

    def test_pause_routes_to_owning_worker(self):
        """暂停必须走 signal_queue,且只发给跑该任务的那个 worker。"""
        s = self._make(2)
        s._task_worker = {"t1": 1}

        s.request_pause("t1")

        s._signal_queues[1].put.assert_called_once()
        s._signal_queues[0].put.assert_not_called()
        s._control_queues[1].put.assert_not_called()

    def test_pause_unknown_task_is_noop(self):
        s = self._make(1)
        s._task_worker = {}
        s.request_pause("ghost")          # 不应抛异常
        s._signal_queues[0].put.assert_not_called()

    def test_pause_pending_task_removes_from_wait_queue(self):
        """尚未派发的任务被暂停后,不能再被派发出去。

        api/tasks.py:842 对 pending 任务只调 request_pause 然后直接落库 paused,
        依赖「之后不会再执行」。若任务留在等待队列里,空闲 worker 一出现就会
        把它跑起来,与库中的 paused 状态矛盾。
        """
        s = self._make(1)
        s._idle = []
        asyncio.run(s.enqueue("t1"))
        self.assertEqual(s._pending, ["t1"])

        s.request_pause("t1")

        self.assertEqual(s._pending, [], "暂停后应从等待队列移除")
        s._signal_queues[0].put.assert_not_called()

    def test_cancel_pending_task_removes_from_wait_queue(self):
        """尚未派发的任务被取消后,不能再被派发(删除流程依赖此行为)。"""
        s = self._make(1)
        s._idle = []
        asyncio.run(s.enqueue("t1"))

        s.request_cancel("t1")

        self.assertEqual(s._pending, [])


class TestSchedulerLifecycle(unittest.TestCase):
    def test_start_spawns_n_workers(self):
        s = Scheduler(num_workers=2)
        with patch("backend.core.scheduler.mp.Process") as proc_cls, \
             patch("backend.core.scheduler.asyncio.create_task"):
            s.start()
        self.assertEqual(proc_cls.call_count, 2)

    def test_shutdown_sends_shutdown_to_each_worker(self):
        s = Scheduler(num_workers=2)
        procs = [MagicMock(), MagicMock()]
        for p in procs:
            p.is_alive.return_value = False
        s._workers = procs
        s._control_queues = [MagicMock(), MagicMock()]
        s._signal_queues = [MagicMock(), MagicMock()]

        s.shutdown()

        for q in s._control_queues:
            q.put.assert_called_with(ControlMessage.shutdown())


class TestEventConsumerDoesNotBlockExit(unittest.TestCase):
    """事件消费协程不得让进程退不掉(回归保护)。

    取事件若用无限期阻塞的 get,默认 executor 的**非守护**线程会永久卡在
    mp.Queue.get,而 asyncio.run 收尾要 join 它 → 进程挂死、Ctrl-C 也无效
    (实测:main 跑完进程仍挂着,只能强杀)。故消费协程必须带超时轮询。

    分两层验证:
      1. 调用签名层(快、失败清晰)——get 必须带有限 timeout;
      2. 子进程端到端层(慢、真起 worker)——真跑一遍确认进程能自己退出。
    第 2 层在缺陷复现时会卡住,故探针脚本内置 faulthandler 强制退出,
    且**把输出重定向到文件而非管道**:子进程会再 spawn 出 worker 孙进程并
    继承句柄,用管道时父进程被 kill 后孙进程仍持有写端,communicate() 读取
    永不结束——测试会挂死而不是失败。
    """

    SCRIPT = textwrap.dedent("""
        import asyncio, os, sys, threading, time
        sys.path.insert(0, {root!r})

        from backend.core.scheduler import Scheduler

        sched = None

        async def main():
            global sched
            sched = Scheduler(num_workers=1)
            sched.start()                 # 真起子进程:消费协程随之跑起来
            await asyncio.sleep(1.5)      # 让它进入阻塞取事件的状态

        def watchdog():
            \"\"\"卡死兜底:把「无限期阻塞」变成可判定的失败。

            缺陷路径下 asyncio.run 收尾会永久 join 默认 executor 线程,主线程
            出不来 —— 探针内的看门狗线程是唯一还能动的执行体。退出前先强杀
            worker:os._exit 跳过 atexit,multiprocessing 的守护子进程清理不会
            执行,不杀的话会留下孤儿进程攥着测试的临时文件句柄。
            \"\"\"
            time.sleep(15)
            print("STUCK_AT_EXIT", file=sys.stderr, flush=True)
            for p in (sched._workers if sched else []):
                try:
                    p.terminate()
                except Exception:
                    pass
            os._exit(3)

        threading.Thread(target=watchdog, daemon=True).start()

        if __name__ == "__main__":
            asyncio.run(main())           # 收尾会 join 默认 executor 线程
            print("EXITED_CLEANLY")
    """)

    def test_event_consumer_polls_get_with_timeout(self):
        """消费协程必须用带超时的 get(签名层,失败信息最直接)。"""
        import queue as queue_mod

        calls = []

        class FakeQueue:
            def get(self, block=True, timeout=None):
                calls.append((block, timeout))
                raise queue_mod.Empty

        s = Scheduler(num_workers=1)
        s._event_queue = FakeQueue()

        async def run_briefly():
            task = asyncio.create_task(s._event_consumer())
            await asyncio.sleep(0.3)
            task.cancel()

        asyncio.run(run_briefly())

        self.assertTrue(calls, "消费协程未取过事件")
        self.assertTrue(
            all(t is not None and t > 0 for _, t in calls),
            f"get 必须带有限超时,实际调用参数:{calls[:3]}。"
            "无限期阻塞会让默认 executor 的线程永不返回,asyncio.run 收尾 "
            "join 它时进程挂死、Ctrl-C 也无效(实测)。",
        )

    def test_process_exits_without_shutdown(self):
        """端到端:真起 worker 后,进程应能自己退出。"""
        root = str(Path(__file__).resolve().parent.parent)
        # 缺陷路径下 worker 孙进程会继承并攥住临时目录里的文件句柄,
        # ignore_cleanup_errors 避免清理失败把真实失败信息盖掉。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            script = Path(d) / "exit_probe.py"
            script.write_text(self.SCRIPT.format(root=root), encoding="utf-8")
            out_path, err_path = Path(d) / "out.txt", Path(d) / "err.txt"
            returncode, stdout, stderr = None, "", ""
            try:
                with open(out_path, "w+", encoding="utf-8",
                          errors="replace") as out_f, \
                     open(err_path, "w+", encoding="utf-8",
                          errors="replace") as err_f:
                    proc = subprocess.run(
                        [sys.executable, str(script)],
                        stdout=out_f, stderr=err_f, timeout=60, cwd=root,
                    )
                    returncode = proc.returncode
                # 读回放最后:文件句柄此时已释放。Windows 上的线程栈 dump
                # 可能含非 UTF-8 字节,故放宽解码。
                stdout = out_path.read_text(encoding="utf-8", errors="replace")
                stderr = err_path.read_text(encoding="utf-8", errors="replace")
            except subprocess.TimeoutExpired:
                self.fail("进程 60s 内未退出,且探针的看门狗也未生效。")

        self.assertNotIn(
            "STUCK_AT_EXIT", stderr,
            "进程未能正常退出 —— 事件消费协程阻塞在无限期的 mp.Queue.get 上,"
            "asyncio.run 收尾 join 默认 executor 线程时被永久卡住。"
            "线程栈:\n" + stderr[-2000:],
        )
        self.assertEqual(returncode, 0, stderr[-2000:])
        self.assertIn("EXITED_CLEANLY", stdout)


if __name__ == "__main__":
    unittest.main()
