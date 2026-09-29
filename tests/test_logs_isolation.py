"""测试进程不得污染用户的生产日志 `data/logs/app.log`。

背景:`backend/core/logs.py` 在 import 时就挂真实文件 handler,于是任何
`import backend.*` 的测试都会把日志写进生产日志。2026-09-29 实测:一次全量
跑完,`app.log` 累计 1464 行测试噪音(占 33045 行的 4.4%),内容是

    2026-09-29 11:28:34 [INFO] 登记服务 测试(imagery):C:\\...\\Temp\\tmp4au2vbav

而"按天分文件、便于翻查"正是需求35 的目标 —— 噪音正好抵消它。

三层防线,本文件各测一层:

  1. **测试进程**(任何调用方式)落到临时目录 —— 判定在 `_resolve_log_dir()`,
     与 `discover tests` / 指定模块 / 脚本方式跑单个文件都无关。
  2. **测试进程 spawn 的子进程**(worker 等)继承同一个临时目录 —— 子进程里
     没有 `unittest`,靠第 1 条会漏;所以测试分支把路径写回 `TDT_LOG_DIR`。
  3. **生产进程**照旧写 `data/logs/app.log` —— 前两条不能把它误伤成"日志不落盘",
     那才是静默丢日志。

断言一律用"内容里有没有那条探针",不用文件大小 —— 用户的服务进程可能同时在
写同一个文件,比大小会 flaky。
"""
import os
import subprocess
import sys
import tempfile
import time
import unittest
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from unittest.mock import patch

from backend.config import ROOT
from backend.core.logs import _resolve_log_dir, logger

#: 用户的生产日志(测试绝不该碰它)
REAL_LOG = ROOT / "data" / "logs" / "app.log"

#: 子进程里探测"文件 handler 挂没挂、落在哪"的代码。
#: **刻意不 import unittest** —— 模拟生产进程(以及测试 spawn 出的 worker)。
_PROBE = (
    "import backend.core.logs as L\n"
    "from logging.handlers import TimedRotatingFileHandler as T\n"
    "hs = [h for h in L.logger.handlers if isinstance(h, T)]\n"
    "print('FILE_HANDLERS=%d' % len(hs))\n"
    "for h in hs:\n"
    "    print('BASE=%s' % h.baseFilename)\n"
)


def _file_handler():
    """取 logger 上那个写文件的 handler;没有则 None。"""
    for h in logger.handlers:
        if isinstance(h, TimedRotatingFileHandler):
            return h
    return None


def _tail_from(path: Path, offset: int) -> str:
    """读 path 中 [offset, 末尾) 的内容(按字节定位,避开文本模式的 tell 限制)。"""
    if not path.exists():
        return ""
    with path.open("rb") as f:
        f.seek(offset)
        return f.read().decode("utf-8", "replace")


def _run_py(code: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """在项目根目录下跑一段 python 代码(env=None 时继承本进程环境)。"""
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                          capture_output=True, text=True, env=env, timeout=180)


class TestResolveLogDir(unittest.TestCase):
    """防线 1 与 3 的判定逻辑(纯函数,三条分支各一测)。"""

    def test_test_process_gets_temp_dir_not_real_dir(self):
        """★ 核心 ★ 测试进程不能落在生产日志目录。"""
        got = _resolve_log_dir()
        self.assertNotEqual(got, ROOT / "data" / "logs",
                            "测试进程仍指向生产日志目录 —— 会污染 app.log")
        self.assertFalse(
            str(got).startswith(str(ROOT / "data")),
            f"测试进程的日志目录 {got} 仍在 data/ 下")

    def test_test_process_publishes_dir_to_env_for_children(self):
        """防线 2:路径要写回 TDT_LOG_DIR,否则 spawn 出的 worker 子进程会漏。"""
        self.assertEqual(Path(os.environ["TDT_LOG_DIR"]), _resolve_log_dir())

    def test_production_process_gets_real_dir(self):
        """防线 3:没有 unittest 时必须是 data/logs —— 别把生产误伤了。"""
        env = {k: v for k, v in os.environ.items() if k != "TDT_LOG_DIR"}
        with patch.dict(os.environ, env, clear=True), \
                patch("backend.core.logs._in_test_process", return_value=False):
            self.assertEqual(_resolve_log_dir(), ROOT / "data" / "logs")

    def test_explicit_override_wins_in_production(self):
        """运维/调试可以显式改日志位置。"""
        tmp = tempfile.mkdtemp(prefix="tdt-override-")
        with patch.dict(os.environ, {"TDT_LOG_DIR": tmp}), \
                patch("backend.core.logs._in_test_process", return_value=False):
            self.assertEqual(_resolve_log_dir(), Path(tmp))

    def test_explicit_override_wins_in_test_process(self):
        """显式覆盖优先于测试判定(想在真实日志里看测试输出时用)。"""
        tmp = tempfile.mkdtemp(prefix="tdt-override-")
        with patch.dict(os.environ, {"TDT_LOG_DIR": tmp}):
            self.assertEqual(_resolve_log_dir(), Path(tmp))


class TestFileHandlerConfig(unittest.TestCase):
    """测试进程仍有文件 handler(只是落在临时目录)—— 否则测试没法验证配置。"""

    def test_handler_present_and_outside_real_dir(self):
        h = _file_handler()
        self.assertIsNotNone(h, "测试进程没有文件 handler —— 轮转配置将无法验证")
        self.assertEqual(Path(h.baseFilename).parent, _resolve_log_dir())
        self.assertNotEqual(Path(h.baseFilename), REAL_LOG)


class TestRealLogNotWrittenByTests(unittest.TestCase):
    """行为验证:测试及其子进程都不该往生产日志加东西。"""

    def test_subprocess_of_test_process_writes_no_real_log(self):
        """★ 防线 2 的核心 ★ 测试 spawn 的 worker 子进程不写生产日志。

        子进程里没有 `unittest`,判定不到"这是测试";靠的是父进程把
        TDT_LOG_DIR 写进了环境变量、被继承下来。
        """
        marker = f"子进程污染探针-{time.time_ns()}"
        before = REAL_LOG.stat().st_size if REAL_LOG.exists() else 0

        r = _run_py(f"import backend.core.logs as L\n"
                    f"L.logger.info({marker!r})\n")
        self.assertEqual(r.returncode, 0, f"子进程失败:{r.stderr}")

        self.assertNotIn(
            marker, _tail_from(REAL_LOG, before),
            "测试的子进程往生产日志写了内容 —— 测试进程仍在污染 app.log")


class TestProductionProcessStillWritesFile(unittest.TestCase):
    """防线 3 的行为验证:生产进程必须真的落盘,别被前两条改成静默丢弃。

    用**不 import unittest** 的子进程模拟生产进程;给它一个临时目录当
    TDT_LOG_DIR,是为了不真污染生产日志 —— 验的是"挂没挂 handler、听谁的"。
    """

    def test_plain_process_attaches_file_handler_at_configured_dir(self):
        tmp = tempfile.mkdtemp(prefix="tdt-prod-probe-")
        env = {k: v for k, v in os.environ.items() if k != "TDT_LOG_DIR"}
        env["TDT_LOG_DIR"] = tmp

        r = _run_py(_PROBE, env=env)
        self.assertEqual(r.returncode, 0, f"子进程失败:{r.stderr}")
        self.assertIn("FILE_HANDLERS=1", r.stdout,
                      f"生产进程没挂文件 handler —— 日志会静默丢失:{r.stdout}{r.stderr}")
        self.assertIn(f"BASE={Path(tmp) / 'app.log'}", r.stdout,
                      f"生产进程的文件 handler 落点不对:{r.stdout}")


if __name__ == "__main__":
    unittest.main()
