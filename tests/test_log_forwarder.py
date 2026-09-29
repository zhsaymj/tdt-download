"""QueueLogHandler 单元测试:验证日志被序列化并投递到队列。"""
import logging
import multiprocessing as mp
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
        try:
            handler = QueueLogHandler(q)
            record = logging.LogRecord(
                name="t", level=logging.WARNING, pathname=__file__, lineno=1,
                msg="磁盘剩余不足", args=(), exc_info=None,
            )
            handler.emit(record)

            # 阻塞等待而非 sleep 赌时序:mp.Queue.put 走后台 feeder 线程,
            # 固定 sleep 在高负载下会 flake;超时即明确失败。
            msg = q.get(timeout=2)
        finally:
            q.close()

        self.assertEqual(msg["kind"], "log")
        self.assertEqual(msg["level"], "WARNING")
        self.assertIn("磁盘剩余不足", msg["msg"])
        self.assertIsInstance(msg["ts"], float)
        # 未指定 worker_id 时回落为空串(单 worker 兼容)
        self.assertEqual(msg["worker_id"], "")

    def test_emit_carries_worker_id(self):
        """worker_id 透传到日志消息,供多 worker 时区分日志来源。"""
        q = mp.Queue()
        try:
            handler = QueueLogHandler(q, worker_id="w3")
            record = logging.LogRecord(
                name="t", level=logging.ERROR, pathname=__file__, lineno=1,
                msg="转换失败", args=(), exc_info=None,
            )
            handler.emit(record)

            msg = q.get(timeout=2)
        finally:
            q.close()

        self.assertEqual(msg["kind"], "log")
        self.assertEqual(msg["level"], "ERROR")
        self.assertEqual(msg["worker_id"], "w3")

    def test_install_forwarding_passes_worker_id(self):
        """install_forwarding 把 worker_id 一路传到 handler。"""
        from backend.core.logs import logger
        q = mp.Queue()
        original = list(logger.handlers)
        original_level = logger.level
        try:
            install_forwarding(logger, q, worker_id="w7")
            self.assertEqual(len(logger.handlers), 1)
            self.assertIsInstance(logger.handlers[0], QueueLogHandler)

            logger.warning("透传校验")
            msg = q.get(timeout=2)
            self.assertEqual(msg["worker_id"], "w7")
            self.assertIn("透传校验", msg["msg"])
        finally:
            logger.handlers[:] = original
            logger.setLevel(original_level)
            q.close()

    def test_setup_after_install_does_not_reattach_file_handler(self):
        """不变式:install 之后再触发 .logs 的 _setup() 不得把文件 handler 挂回来。

        在全新子进程中验证 —— 本进程内 .logs 早已导入、_tdt_ready 已置位,
        无法复现"先 install 后 setup"这一危险顺序。子进程里:
          1) 先 import log_forwarder 并 install_forwarding;
          2) 再首次 import backend.core.logs(会执行模块级 _setup());
          3) 若 _setup() 仍会挂载 RotatingFileHandler,说明转发被静默覆盖。
        断言 handler 仍只有转发 handler。
        """
        import subprocess

        root = str(Path(__file__).parent.parent)
        code = (
            "import logging, multiprocessing as mp, sys;"
            "sys.path.insert(0, " + repr(root) + ");"
            "from backend.core.log_forwarder import install_forwarding, QueueLogHandler;"
            "q = mp.Queue();"
            "lg = logging.getLogger('tdt');"
            "install_forwarding(lg, q, 'w9');"
            "from backend.core import logs as _l;"
            "from backend.core.logs import logger;"
            "assert lg is logger;"
            "hs = [type(h).__name__ for h in logger.handlers];"
            "print('HANDLERS=' + ','.join(hs));"
            "print('COUNT=' + str(len(logger.handlers)));"
        )

        proc = subprocess.run([sys.executable, "-c", code],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0,
                         f"子进程失败:{proc.stderr}")
        self.assertIn("COUNT=1", proc.stdout,
                      f"转发 handler 应仍唯一存在,实际输出:{proc.stdout}")
        self.assertIn("QueueLogHandler", proc.stdout,
                      f"handler 应为 QueueLogHandler,实际输出:{proc.stdout}")
        self.assertNotIn("RotatingFileHandler", proc.stdout,
                         f"_setup() 不应把文件 handler 挂回来:{proc.stdout}")

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
