"""日志按日期轮转,以及"面板不受轮转影响"的行为护栏。

背景:原实现是 RotatingFileHandler(maxBytes=5MB, backupCount=3),但实测
26 个使用日只累积 3.2MB、最大单日 686KB —— 5MB 阈值要 26 天才转一次,
文件会跨月累积。改为按日期(TimedRotatingFileHandler, midnight)。

多进程隐患不适用本项目:worker 的 handler 被 install_forwarding 清空换成
队列转发,只有主进程写文件(见 tests/test_log_forwarder.py 的护栏)。

⚠️ 测试进程里文件 handler 落在临时目录,不是 `data/logs/` —— 判定见
   `core/logs.py::_resolve_log_dir`(测试进程不许污染生产日志)。
   本文件验的是 handler 的**配置**(类型/轮转点/保留期/编码),与落点无关;
   落点由 tests/test_logs_isolation.py 三层守着。
"""
import os
import shutil
import tempfile
import time
import unittest
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from backend.core.logs import logger, recent_logs


def _file_handler():
    """取 logger 上那个写文件的 handler;没有则 None。"""
    for h in logger.handlers:
        if isinstance(h, TimedRotatingFileHandler):
            return h
    return None


class TestRotationConfig(unittest.TestCase):
    def test_uses_timed_rotating_handler(self):
        """★ 核心 ★ 必须是按日期的 handler,不是按大小的。"""
        from logging.handlers import RotatingFileHandler
        h = _file_handler()
        self.assertIsNotNone(h, "未找到文件 handler —— 日志不会落盘")
        self.assertNotIsInstance(h, RotatingFileHandler,
                                 "仍是按大小轮转(RotatingFileHandler)")

    def test_when_is_midnight(self):
        """rotation at midnight(标准库把 when 存成大写,故比较时统一小写)。"""
        h = _file_handler()
        self.assertIsNotNone(h)
        self.assertEqual(h.when.lower(), "midnight",
                         "轮转点应为每天零点,生成 app.log.<日期>")

    def test_backup_count_is_180(self):
        h = _file_handler()
        self.assertIsNotNone(h)
        self.assertEqual(h.backupCount, 180, "保留期应为 180 天")

    def test_utc_is_false(self):
        """用本机时间 —— 与界面上的日志时间戳一致。
        设 True 会让轮转点与文件名日期错开 8 小时。"""
        h = _file_handler()
        self.assertIsNotNone(h)
        self.assertFalse(h.utc)

    def test_encoding_is_utf8(self):
        h = _file_handler()
        self.assertIsNotNone(h)
        self.assertEqual(h.encoding, "utf-8",
                         "日志含中文,非 utf-8 会乱码/报错")

    def test_base_filename_is_app_log_in_configured_dir(self):
        """文件名恒为 app.log,目录听 `TDT_LOG_DIR`(生产下即 data/logs/)。

        "生产进程落在 data/logs/app.log"这条由 tests/test_logs_isolation.py 的
        TestProductionProcessStillWritesFile 用子进程守着 —— 那边不受测试引导影响。
        """
        h = _file_handler()
        self.assertIsNotNone(h)
        self.assertEqual(Path(h.baseFilename).name, "app.log")
        self.assertEqual(Path(h.baseFilename).parent,
                         Path(os.environ["TDT_LOG_DIR"]))


class TestPanelUnaffectedByRotation(unittest.TestCase):
    """★ 需求明确要求 ★ 不管怎么新建文件,最新日志都要在运行日志面板展示。

    面板走的是内存环形缓冲(/api/logs → recent_logs → _ring),不是文件 ——
    这条测的就是这个解耦不被破坏。用行为验证而非断言源码。

    ⚠️ 刻意用**临时文件 + 临时 handler**,而不是真实的 data/logs/app.log:
      1) 真实日志文件通常被正在运行的服务进程持有句柄,Windows 下改名会抛
         PermissionError [WinError 32](实测踩到:本机服务占用时无法轮转)
      2) 测试不该把标记写进用户的真实日志,更不该轮转它
    临时文件只有本进程持有,轮转必定成功,验的仍是"面板不依赖文件内容"。
    """

    def test_recent_logs_survives_file_rotation(self):
        tmp = Path(tempfile.mkdtemp())
        tmp_log = tmp / "app.log"
        h = TimedRotatingFileHandler(tmp_log, when="midnight",
                                     backupCount=180, encoding="utf-8")
        logger.addHandler(h)
        try:
            marker = f"轮转护栏-{time.time_ns()}"
            logger.info(marker)
            self.assertIn(marker, [x["msg"] for x in recent_logs(50)],
                          "写入后立刻应能在面板可见")
            self.assertTrue(tmp_log.exists(), "临时文件未落盘,前提不成立")

            # 轮转:原文件被改名(正是真实轮转做的动作)
            h.doRollover()

            # 先确认轮转**确实发生了** —— 否则下面那条会真空转通过
            rotated = [p.name for p in tmp.iterdir()
                       if p.name.startswith("app.log.")]
            self.assertTrue(rotated,
                            f"doRollover 未产生轮转文件,本用例无法验证解耦:"
                            f"{sorted(p.name for p in tmp.iterdir())}")

            # 文件已轮转,面板仍须能看到刚才那条
            self.assertIn(marker, [x["msg"] for x in recent_logs(50)],
                          "日志文件被轮转后,面板读不到最新日志 —— 解耦被破坏")
        finally:
            logger.removeHandler(h)
            h.close()
            shutil.rmtree(tmp, ignore_errors=True)

    def test_recent_logs_reads_memory_not_file(self):
        """更直接的证明:临时 handler 写完并**删掉文件**后,面板条目仍在。

        面板若不读文件,删文件必然不影响它;反之这条会红。
        """
        tmp = Path(tempfile.mkdtemp())
        tmp_log = tmp / "app.log"
        h = TimedRotatingFileHandler(tmp_log, when="midnight",
                                     backupCount=180, encoding="utf-8")
        logger.addHandler(h)
        try:
            marker = f"删文件护栏-{time.time_ns()}"
            logger.info(marker)
            h.close()
            logger.removeHandler(h)          # 先摘掉 handler 释放句柄
            shutil.rmtree(tmp, ignore_errors=True)

            self.assertIn(marker, [x["msg"] for x in recent_logs(50)],
                          "日志文件被删除后,面板读不到最新日志 "
                          "—— 说明面板在读文件,解耦被破坏")
        finally:
            if h in logger.handlers:
                logger.removeHandler(h)
            shutil.rmtree(tmp, ignore_errors=True)


class TestRepeatedSetupIsIdempotent(unittest.TestCase):
    """_setup() 由 _tdt_ready 守卫,不该重复挂 handler。

    重复挂会让每条日志写两遍(文件里出现重复行)。
    """

    def test_setup_twice_adds_no_handler(self):
        from backend.core.logs import _setup
        before = len(logger.handlers)
        _setup()
        _setup()
        self.assertEqual(len(logger.handlers), before,
                         "_setup() 不幂等 —— 重复调用会重复挂 handler")


if __name__ == "__main__":
    unittest.main()
