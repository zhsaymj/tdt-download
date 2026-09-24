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
