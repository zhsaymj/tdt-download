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
        """省略 worker_id 时回落空串,兼容单 worker 场景。"""
        msg = EventMessage.log("INFO", "test message", 1234567890.0)
        self.assertEqual(msg, {
            "kind": "log",
            "level": "INFO",
            "msg": "test message",
            "ts": 1234567890.0,
            "worker_id": ""
        })

    def test_event_log_encode_with_worker_id(self):
        """显式传 worker_id 时进入消息,供多 worker 区分日志来源。"""
        msg = EventMessage.log("ERROR", "boom", 1.0, worker_id="w2")
        self.assertEqual(msg, {
            "kind": "log",
            "level": "ERROR",
            "msg": "boom",
            "ts": 1.0,
            "worker_id": "w2"
        })

    def test_event_finished_encode(self):
        msg = EventMessage.finished("task-abc")
        self.assertEqual(msg, {"kind": "finished", "task_id": "task-abc"})

    def test_parse_control_run(self):
        result = parse_control({"kind": "run", "task_id": "t1"})
        self.assertEqual(result, ("run", {"task_id": "t1"}))

    def test_parse_control_pause(self):
        result = parse_control({"kind": "pause", "task_id": "t2"})
        self.assertEqual(result, ("pause", {"task_id": "t2"}))

    def test_parse_control_cancel(self):
        result = parse_control({"kind": "cancel", "task_id": "t3"})
        self.assertEqual(result, ("cancel", {"task_id": "t3"}))

    def test_parse_control_shutdown(self):
        result = parse_control({"kind": "shutdown"})
        self.assertEqual(result, ("shutdown", {}))

    def test_parse_control_missing_kind(self):
        with self.assertRaises(ValueError) as cm:
            parse_control({})
        self.assertIn("Missing 'kind' field", str(cm.exception))

    def test_parse_control_unknown_kind(self):
        with self.assertRaises(ValueError) as cm:
            parse_control({"kind": "unknown"})
        self.assertIn("Unknown control kind", str(cm.exception))

    def test_parse_event_event(self):
        result = parse_event({"kind": "event", "payload": {"type": "task"}})
        self.assertEqual(result, ("event", {"payload": {"type": "task"}}))

    def test_parse_event_log(self):
        result = parse_event({"kind": "log", "level": "ERROR", "msg": "fail", "ts": 123.0})
        self.assertEqual(result, ("log", {"level": "ERROR", "msg": "fail", "ts": 123.0}))

    def test_parse_event_finished(self):
        result = parse_event({"kind": "finished", "task_id": "t-done"})
        self.assertEqual(result, ("finished", {"task_id": "t-done"}))

    def test_parse_event_missing_kind(self):
        with self.assertRaises(ValueError) as cm:
            parse_event({})
        self.assertIn("Missing 'kind' field", str(cm.exception))

    def test_parse_event_unknown_kind(self):
        with self.assertRaises(ValueError) as cm:
            parse_event({"kind": "invalid"})
        self.assertIn("Unknown event kind", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
