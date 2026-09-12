"""processor 适配层基类测试。

全部用 ``sys.executable -c`` 构造假命令(打印进度行/sleep/非零退出/stderr 输出),
不依赖真实外部工具(3dtiles/pdal/py3dtiles)。
"""
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from backend.core.processors.base import (
    BaseProcessor,
    ProcessorCancelled,
    ProcessorError,
    run_cli,
)


def _py(script: str) -> list[str]:
    return [sys.executable, "-c", script]


class _FakeProcessor(BaseProcessor):
    """打印 PROGRESS xx(百分比)的假处理器,产物为 out_dir/out.txt。"""

    name = "fake"

    def check_available(self, cfg):
        return True, ""

    def build_cmd(self, *args, **kwargs):
        return []

    def parse_progress(self, line):
        if line.startswith("PROGRESS "):
            return float(line.split()[1]) / 100.0
        return None

    def expected_outputs(self, out_dir):
        return [Path(out_dir) / "out.txt"]


class ProcessorErrorTest(unittest.TestCase):
    """ProcessorError 归一化:退出码 / stderr 截断 / 中文消息。"""

    def test_nonzero_exit_raises_with_code_stderr_and_chinese_message(self):
        script = "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"
        with self.assertRaises(ProcessorError) as cm:
            run_cli(_py(script))
        err = cm.exception
        self.assertEqual(err.exit_code, 3)
        self.assertIn("boom", err.stderr)
        # 面向用户的中文消息:工具名 + 退出码
        self.assertIn("退出码 3", str(err))

    def test_stderr_truncated_to_last_2000_chars(self):
        # 输出 5000 字符,只保留末尾 2000(前部 A 被截掉,末尾 B 保留)
        script = (
            "import sys\n"
            "sys.stderr.write('A' * 3000 + 'B' * 2000 + '\\n')\n"
            "sys.exit(1)\n"
        )
        with self.assertRaises(ProcessorError) as cm:
            run_cli(_py(script))
        err = cm.exception
        self.assertLessEqual(len(err.stderr), 2000)
        self.assertIn("B" * 100, err.stderr)
        self.assertNotIn("A" * 100, err.stderr)


class RunCliCancelTest(unittest.TestCase):
    """取消事件:threading.Event 置位后进程被 terminate,抛 ProcessorCancelled。"""

    def test_cancel_event_terminates_process(self):
        cancel = threading.Event()
        threading.Timer(0.3, cancel.set).start()
        t0 = time.monotonic()
        with self.assertRaises(ProcessorCancelled):
            run_cli(_py("import time; time.sleep(60)"), cancel_event=cancel)
        # 被 terminate(而非跑满 60s);terminate 失败兜底 kill 至多再等 5s
        self.assertLess(time.monotonic() - t0, 10)

    def test_cancel_via_processor_run_propagates(self):
        cancel = threading.Event()
        cancel.set()  # 已置位 → 启动后立即取消
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ProcessorCancelled):
                _FakeProcessor().run(
                    _py("import time; time.sleep(60)"),
                    out_dir=d, cancel_event=cancel)


class RunCliOutputTest(unittest.TestCase):
    """stdout/stderr 行回调:进度解析与日志透传;编码容错。"""

    def test_stdout_line_callback_drives_progress(self):
        lines = []
        script = (
            "for i in range(1, 5):\n"
            "    print('PROGRESS %d' % (i * 25), flush=True)\n"
        )
        result = run_cli(_py(script), on_stdout_line=lines.append)
        self.assertTrue(result.ok)
        self.assertEqual(lines, ["PROGRESS 25", "PROGRESS 50",
                                 "PROGRESS 75", "PROGRESS 100"])

    def test_stderr_line_callback(self):
        lines = []
        run_cli(_py("import sys; sys.stderr.write('e1\\ne2\\n')"),
                on_stderr_line=lines.append)
        self.assertEqual(lines, ["e1", "e2"])

    def test_stdout_and_stderr_read_concurrently_without_deadlock(self):
        # 两路都输出超过管道缓冲容量的数据,单线程顺序读会死锁
        script = (
            "import sys\n"
            "for i in range(2000):\n"
            "    print('o' * 60, flush=True)\n"
            "    sys.stderr.write('e' * 60 + '\\n')\n"
            "    sys.stderr.flush()\n"
        )
        out_lines, err_lines = [], []
        result = run_cli(_py(script),
                         on_stdout_line=out_lines.append,
                         on_stderr_line=err_lines.append,
                         timeout=30)
        self.assertTrue(result.ok)
        self.assertEqual(len(out_lines), 2000)
        self.assertEqual(len(err_lines), 2000)

    def test_gbk_output_decoded(self):
        # Windows 外部工具常按 GBK 输出中文,utf-8 解码失败应回落 gbk
        script = ("import sys\n"
                  "sys.stdout.buffer.write('处理完成'.encode('gbk') + b'\\n')\n")
        lines = []
        run_cli(_py(script), on_stdout_line=lines.append)
        self.assertEqual(lines, ["处理完成"])

    def test_utf8_output_decoded(self):
        script = ("import sys\n"
                  "sys.stdout.buffer.write('进度 50%'.encode('utf-8') + b'\\n')\n")
        lines = []
        run_cli(_py(script), on_stdout_line=lines.append)
        self.assertEqual(lines, ["进度 50%"])

    def test_timeout_kills_and_raises_chinese_message(self):
        t0 = time.monotonic()
        with self.assertRaises(ProcessorError) as cm:
            run_cli(_py("import time; time.sleep(60)"), timeout=0.5)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIn("超时", str(cm.exception))


class BaseProcessorRunTest(unittest.TestCase):
    """BaseProcessor.run:进度解析驱动回调 + 产物存在性/非空校验。"""

    def test_run_collects_outputs_and_progress(self):
        with tempfile.TemporaryDirectory() as d:
            script = (
                "import pathlib\n"
                "for i in (25, 50, 75, 100):\n"
                "    print('PROGRESS %d' % i, flush=True)\n"
                f"pathlib.Path(r'{d}').joinpath('out.txt')"
                ".write_text('done', encoding='utf-8')\n"
            )
            progress = []
            result = _FakeProcessor().run(
                _py(script), out_dir=d, on_progress=progress.append)
            self.assertTrue(result.ok)
            self.assertEqual(progress, [0.25, 0.5, 0.75, 1.0])
            self.assertEqual(result.outputs, [str(Path(d) / "out.txt")])

    def test_run_passthrough_stdout_lines_and_progress(self):
        # on_stdout_line 收到全部 stdout 行(含无法解析进度的行),
        # on_progress 只收到解析成功的进度
        with tempfile.TemporaryDirectory() as d:
            script = (
                "import pathlib\n"
                "print('some log line', flush=True)\n"
                "print('PROGRESS 50', flush=True)\n"
                f"pathlib.Path(r'{d}').joinpath('out.txt')"
                ".write_text('done', encoding='utf-8')\n"
            )
            lines, progress = [], []
            result = _FakeProcessor().run(
                _py(script), out_dir=d,
                on_stdout_line=lines.append, on_progress=progress.append)
            self.assertTrue(result.ok)
            self.assertEqual(lines, ["some log line", "PROGRESS 50"])
            self.assertEqual(progress, [0.5])

    def test_run_stdout_line_callback_exception_still_completes(self):
        # 透传回调抛异常不应杀死读取线程:run 正常完成,
        # 且所有行都送达过回调(用计数列表验证行数)
        with tempfile.TemporaryDirectory() as d:
            script = (
                "import pathlib\n"
                "for i in range(3):\n"
                "    print('line %d' % i, flush=True)\n"
                f"pathlib.Path(r'{d}').joinpath('out.txt')"
                ".write_text('done', encoding='utf-8')\n"
            )
            seen = []

            def cb(line):
                seen.append(line)
                raise RuntimeError("boom")

            result = _FakeProcessor().run(
                _py(script), out_dir=d, on_stdout_line=cb)
            self.assertTrue(result.ok)
            self.assertEqual(seen, ["line 0", "line 1", "line 2"])

    def test_run_missing_output_raises_chinese_error(self):
        with tempfile.TemporaryDirectory() as d:
            # 进程正常结束(退出码 0)但没产出预期产物 → ProcessorError
            with self.assertRaises(ProcessorError) as cm:
                _FakeProcessor().run(_py("print('PROGRESS 100')"), out_dir=d)
            self.assertIn("产物", str(cm.exception))
            self.assertIn("out.txt", str(cm.exception))

    def test_run_empty_output_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            # 产物存在但为 0 字节 → 视为缺失
            script = f"pathlib.Path(r'{d}').joinpath('out.txt').touch()\n"
            script = "import pathlib\n" + script
            with self.assertRaises(ProcessorError) as cm:
                _FakeProcessor().run(_py(script), out_dir=d)
            self.assertIn("产物", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
