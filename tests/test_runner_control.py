"""验证三个 runner 都接受 should_stop 参数,且不再依赖 task_queue 单例。"""
import inspect
import unittest
from unittest import mock


class TestRunnerSignatures(unittest.TestCase):
    def test_run_task_accepts_should_stop(self):
        from backend.core.runner import run_task
        params = list(inspect.signature(run_task).parameters)
        self.assertEqual(params, ["task_id", "emit", "should_stop"])

    def test_run_3d_task_accepts_should_stop(self):
        from backend.core.runner_3d import run_task
        params = list(inspect.signature(run_task).parameters)
        self.assertEqual(params, ["task_id", "emit", "should_stop"])

    def test_run_buildings_task_accepts_should_stop(self):
        from backend.core.runner_buildings import run_buildings_task
        params = list(inspect.signature(run_buildings_task).parameters)
        self.assertEqual(params, ["task_id", "emit", "should_stop"])


class TestNoQueueDependency(unittest.TestCase):
    def test_runners_do_not_import_task_queue(self):
        """三个 runner 源码中不应再有 task_queue.control_of 调用。"""
        from pathlib import Path
        core = Path(__file__).resolve().parent.parent / "backend" / "core"
        for name in ("runner.py", "runner_3d.py", "runner_buildings.py"):
            src = (core / name).read_text(encoding="utf-8")
            self.assertNotIn(
                "task_queue.control_of", src,
                f"{name} 仍在直接读 task_queue 单例,子进程中读不到主进程状态",
            )


class TestHandleStopReason(unittest.TestCase):
    """_handle_stop 必须区分「暂停」与「取消」。

    只看 should_stop() 的真假是不够的:暂停与取消都会让 should_stop() 为真,
    若一律按取消落库,用户点「暂停」(api_pause_task 已回 "已暂停")却看到
    「已取消」,并以为任务被丢弃。故停止原因取自 should_stop() 的返回值:
    返回 "cancel" 才落 canceled,其余("pause" 或纯 bool)一律落 paused
    —— bool-only 闭包区分不了两者,按可恢复的暂停落库是安全的一侧。
    """

    def _run_2d(self, reason):
        from backend.core.runner import _handle_stop
        events: list = []
        calls: list = []
        tracker = mock.Mock()
        with mock.patch("backend.core.runner.update_task",
                        side_effect=lambda *a, **kw: calls.append(kw)):
            _handle_stop("t1", tracker, "download", 1, 0, 10,
                         events.append, lambda: reason)
        return calls[0]["status"], events[0]["status"]

    def _run_3d(self, reason, module, func_name):
        import importlib
        mod = importlib.import_module(module)
        events: list = []
        calls: list = []
        tracker = mock.Mock()
        with mock.patch.object(mod, "update_task",
                              side_effect=lambda *a, **kw: calls.append(kw)):
            getattr(mod, func_name)("t1", tracker, "convert_3d",
                                    events.append, lambda: reason)
        return calls[0]["status"], events[0]["status"]

    def test_pause_reason_lands_paused(self):
        self.assertEqual(self._run_2d("pause"), ("paused", "paused"))

    def test_cancel_reason_lands_canceled(self):
        self.assertEqual(self._run_2d("cancel"), ("canceled", "canceled"))

    def test_bool_only_closure_defaults_to_paused(self):
        self.assertEqual(self._run_2d(True), ("paused", "paused"))

    def test_3d_pause_reason_lands_paused(self):
        self.assertEqual(
            self._run_3d("pause", "backend.core.runner_3d", "_handle_stop"),
            ("paused", "paused"))

    def test_3d_cancel_reason_lands_canceled(self):
        self.assertEqual(
            self._run_3d("cancel", "backend.core.runner_3d", "_handle_stop"),
            ("canceled", "canceled"))

    def test_buildings_pause_reason_lands_paused(self):
        self.assertEqual(
            self._run_3d("pause", "backend.core.runner_buildings",
                         "_handle_stop"),
            ("paused", "paused"))

    def test_buildings_cancel_reason_lands_canceled(self):
        self.assertEqual(
            self._run_3d("cancel", "backend.core.runner_buildings",
                         "_handle_stop"),
            ("canceled", "canceled"))


if __name__ == "__main__":
    unittest.main()
