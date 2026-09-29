"""验证 worker 按 provider 分发到正确的 runner、运行期暂停生效、坏消息不杀进程。"""
import queue as queue_mod
import unittest
from pathlib import Path
import sys
from unittest.mock import patch

# 添加项目根目录到 sys.path(与 test_worker.py 一致,供直接以脚本方式运行)
sys.path.insert(0, str(Path(__file__).parent.parent))

# 跨测试模块复用 logger 快照还原(与 test_worker.py 同一份实现,避免抄第二遍)。
# tests/ 无 __init__.py,靠命名空间包导入,故必须先补齐项目根的 sys.path。
from tests.test_worker import _LoggerSnapshotMixin


def _drain(q) -> list:
    """非阻塞取出队列里全部消息。"""
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


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
            # 必须是专用异常,不能是 LookupError:后者是 KeyError/IndexError
            # 的基类,会让 runner 里 task["bbox"] 之类的取值异常被误判成
            # 「任务不存在」,静默吞掉(见 _run_task 的分支说明)。
            with self.assertRaises(worker.TaskNotFoundError):
                worker._resolve_runner("ghost")


class TestRunTaskErrorBranches(_LoggerSnapshotMixin, unittest.TestCase):
    """`_run_task` 的异常分支必须可区分:任务不存在 vs 任务执行异常。"""

    def _run_with_runner_raising(self, exc_factory):
        """跑一轮 worker_main(进程内),runner 抛指定异常,返回事件列表。"""
        from backend.core import worker as worker_mod
        from backend.core.messages import ControlMessage

        async def boom(task_id, emit, should_stop):
            raise exc_factory()

        control_q = queue_mod.Queue()
        signal_q = queue_mod.Queue()
        event_q = queue_mod.Queue()
        control_q.put(ControlMessage.run("t1"))
        control_q.put(ControlMessage.shutdown())

        with patch.object(worker_mod, "get_task",
                          return_value={"id": "t1", "provider": "tianditu_img"}), \
             patch.object(worker_mod, "_resolve_runner", lambda _tid: boom):
            worker_mod.worker_main(control_q, signal_q, event_q, "w0")

        return _drain(event_q)

    def test_runner_keyerror_uses_generic_failure_branch(self):
        """runner 里抛的 KeyError 必须走通用失败分支(投 failed 事件)。

        LookupError 是 KeyError/IndexError 的基类。若用它来表示「任务不存在」,
        runner 里 task["bbox"] 这类取值异常会被误判成任务不存在 —— 该分支既不
        落库也不投 failed 事件,任务永远停在 running,前端显示「运行中」,而
        Task 6 收到 finished 就回收 worker slot:任务变成不推进、不失败、不解除
        的幽灵。
        """
        events = self._run_with_runner_raising(lambda: KeyError("bbox"))

        failed = [e for e in events
                  if e["kind"] == "event"
                  and e["payload"].get("status") == "failed"]
        self.assertEqual(len(failed), 1, f"应投 failed 事件,实际:{events}")
        self.assertIn("bbox", failed[0]["payload"]["message"])

        # 且不能被当成「任务不存在」静默吞掉
        self.assertFalse(
            [e for e in events
             if e["kind"] == "log" and "任务不存在" in e.get("msg", "")],
            f"KeyError 被误判为任务不存在:{events}",
        )
        # 真实异常要留下 traceback(通用分支投 error 日志),便于定位
        self.assertTrue(
            [e for e in events
             if e["kind"] == "log" and e.get("level") == "error"
             and "Traceback" in e.get("msg", "")],
            f"应留下带 traceback 的 error 日志:{events}",
        )

    def test_runner_indexerror_uses_generic_failure_branch(self):
        """IndexError 同理:它是 LookupError 的另一个子类。"""
        events = self._run_with_runner_raising(lambda: IndexError("empty list"))
        failed = [e for e in events
                  if e["kind"] == "event"
                  and e["payload"].get("status") == "failed"]
        self.assertEqual(len(failed), 1, f"应投 failed 事件,实际:{events}")


class TestMalformedMessages(_LoggerSnapshotMixin, unittest.TestCase):
    """坏消息不得杀死 worker / 信号线程。

    parse_control 只校验 kind,不校验各 kind 的必填字段,故 {"kind":"run"} 这类
    缺字段消息能过校验。改造前的 _control_loop 用 try 包住整个循环体(注释原话
    「消息解析或处理异常不应崩溃 worker」),收窄成只包 parse_control 即回归:
    取值处抛 KeyError 冒泡出 worker_main → worker 进程死亡,而 Task 6 不做
    is_alive 健康检查、往死进程的 Queue put 也会成功 → 任务被静默投进死 worker
    且无超时;信号线程同理更隐蔽(死了连日志都没有,暂停/取消永久失效)。
    """

    def test_malformed_run_message_keeps_worker_alive(self):
        from backend.core import worker as worker_mod
        from backend.core.messages import ControlMessage

        control_q = queue_mod.Queue()
        signal_q = queue_mod.Queue()
        event_q = queue_mod.Queue()
        control_q.put({"kind": "run"})               # 坏消息:缺 task_id
        control_q.put(ControlMessage.run("ghost"))   # 后续正常消息仍须被处理
        control_q.put(ControlMessage.shutdown())

        worker_mod.worker_main(control_q, signal_q, event_q, "w0")

        events = _drain(event_q)
        self.assertTrue(
            [e for e in events
             if e["kind"] == "log" and "非法" in e.get("msg", "")],
            f"坏消息应被忽略并留日志:{events}",
        )
        self.assertTrue(
            [e for e in events
             if e["kind"] == "finished" and e["task_id"] == "ghost"],
            f"后续 run 消息应仍被处理(worker 存活):{events}",
        )


class TestPauseDuringRun(_LoggerSnapshotMixin, unittest.TestCase):
    """回归保护:任务运行期间 pause 必须生效。

    若把信号接收和任务执行放回同一个线程,pause 消息会一直留在队列里
    直到任务自己跑完 —— 暂停功能静默失效(已实测复现)。

    用线程而非子进程跑 worker_main:patch 无法跨 spawn 子进程(子进程重新
    import 模块,父进程的补丁不生效),而这里要验证的"信号线程能否在任务
    阻塞时收到信号"本就是线程级问题,不需要真进程。子进程版端到端验证
    留给 Task 8。

    顺带覆盖「坏信号不杀信号线程」:先投一条缺 task_id 的 pause,若信号线程
    因此死掉,后面的正常 pause 就再也不会生效 —— 该故障没有任何外部可观测
    信号,只能靠这条断言守住。
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
                signal_q.put({"kind": "pause"})    # 坏信号:缺 task_id
                time.sleep(0.2)                    # 给它被处理的机会
                signal_q.put(ControlMessage.pause("t1"))

                try:
                    stopped = seen_stop.get(timeout=8)
                except queue_mod.Empty:
                    stopped = None
                self.assertTrue(stopped, "任务未在收到 pause 后停止")

                # 信号线程必须还活着:坏信号被记了一笔,而不是把它带走
                logs = _drain(event_q)
                self.assertTrue(
                    [e for e in logs
                     if e["kind"] == "log" and "非法" in e.get("msg", "")],
                    f"坏信号应被忽略并留日志(信号线程存活):{logs}",
                )
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
