"""QueueLogHandler 单元测试:验证日志被序列化并投递到队列。"""
import logging
import multiprocessing as mp
import time
import unittest
from pathlib import Path
import sys

# 添加项目根目录到 sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from backend.core.log_forwarder import QueueLogHandler, install_forwarding


class TestQueueLogHandler(unittest.TestCase):
    def test_emit_puts_log_message(self):
        """一条日志被转成 EventMessage.log 投进队列。"""
        q = mp.Queue()
        handler = QueueLogHandler(q)
        record = logging.LogRecord(
            name="t", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="磁盘剩余不足", args=(), exc_info=None,
        )
        handler.emit(record)
        time.sleep(0.05)

        self.assertFalse(q.empty(), "队列应有日志消息")
        msg = q.get_nowait()
        self.assertEqual(msg["kind"], "log")
        self.assertEqual(msg["level"], "WARNING")
        self.assertIn("磁盘剩余不足", msg["msg"])
        self.assertIsInstance(msg["ts"], float)

    def test_install_forwarding_replaces_handlers(self):
        """install_forwarding 清空原 handler 并只留转发 handler。"""
        from backend.core.logs import logger
        q = mp.Queue()
        original = list(logger.handlers)
        try:
            install_forwarding(logger, q)
            self.assertEqual(len(logger.handlers), 1)
            self.assertIsInstance(logger.handlers[0], QueueLogHandler)
        finally:
            logger.handlers[:] = original

    def test_handler_error_does_not_raise(self):
        """队列损坏时 emit 不抛异常(日志不该拖垮业务)。"""
        class BrokenQueue:
            def put(self, _):
                raise RuntimeError("queue closed")
        handler = QueueLogHandler(BrokenQueue())
        record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None)
        handler.emit(record)  # 不应抛异常


if __name__ == "__main__":
    unittest.main()
