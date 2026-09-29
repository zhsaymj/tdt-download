"""集中式运行日志:滚动文件 + 内存环形缓冲 + WebSocket 实时推送。

三处出口便于排查"是不是卡住了":
  1) 滚动文件 {ROOT}/data/logs/app.log(重启保留,可事后翻查)
  2) 内存环形缓冲(最近 N 条,供 GET /api/logs 拉取,页面打开即见历史)
  3) 注入的 notifier 回调(把每条日志经队列广播给前端,实时刷新)

用法:from .logs import logger; logger.info("...").
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from collections import deque
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

from ..config import ROOT

# 环形缓冲容量(最近多少条日志常驻内存供页面拉取)
_RING_CAP = 800

logger = logging.getLogger("tdt")
logger.setLevel(logging.INFO)
logger.propagate = False


class _RingHandler(logging.Handler):
    """把日志同时存进环形缓冲、并推给可选的 notifier(WS 广播)。"""

    def __init__(self):
        super().__init__()
        self.buffer: deque[dict] = deque(maxlen=_RING_CAP)
        self._notifier = None  # callable(dict) -> None,由 main 注入

    def set_notifier(self, cb):
        self._notifier = cb

    def emit(self, record: logging.LogRecord):
        try:
            item = {
                "ts": datetime.fromtimestamp(record.created).strftime("%H:%M:%S"),
                "level": record.levelname,
                "msg": record.getMessage(),
            }
            self.buffer.append(item)
            if self._notifier:
                try:
                    self._notifier({"type": "log", **item})
                except Exception:
                    pass
        except Exception:
            pass

    def recent(self, limit: int = 300) -> list[dict]:
        items = list(self.buffer)
        return items[-limit:] if limit and limit > 0 else items


_ring = _RingHandler()


def _in_test_process() -> bool:
    """本进程是不是测试进程。

    单抽成一个函数只为可测:测试里要能分别验"生产分支"与"测试分支",
    而直接增删 `sys.modules["unittest"]` 会连测试框架自己一起拆掉。
    """
    return "unittest" in sys.modules


def _resolve_log_dir() -> Path:
    """解析日志目录。

    **测试进程落到临时目录**,否则跑一次测试就会把日志写进用户的生产日志
    `data/logs/app.log` —— 2026-09-29 实测一次全量灌进 1464 行噪音(占
    33045 行的 4.4%),正好抵消需求35"按天分文件、便于翻查"的目标。

    判定用 `_in_test_process()`,与**调用方式无关**:
    `discover tests` / `python -m unittest tests.test_x` / 脚本方式跑单个文件
    三种都成立。(最初把引导放在 `tests/__init__.py`,结果 `discover tests` 下
    模块被当成顶层模块导入、那个文件根本不执行 —— 所以防线必须在这里。)

    测试分支还会把路径写回 `TDT_LOG_DIR`,好让被测代码 spawn 出去的 worker
    子进程继承同一个目录 —— 子进程里没有 `unittest`,单靠上面的判定会漏。

    ⚠️ 别改成"测试干脆不落盘":文件 handler 没了,测试就没法验证它的配置
       (轮转点/保留期/编码)。tests/test_logs_isolation.py 守着这三层。
    """
    override = os.environ.get("TDT_LOG_DIR")
    if override:
        return Path(override)
    if _in_test_process():
        if "TDT_LOG_DIR" not in os.environ:
            os.environ["TDT_LOG_DIR"] = tempfile.mkdtemp(prefix="tdt-test-logs-")
        return Path(os.environ["TDT_LOG_DIR"])
    return ROOT / "data" / "logs"


def _setup() -> None:
    """初始化文件与环形处理器(幂等,重复调用只装一次)。"""
    if getattr(logger, "_tdt_ready", False):
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        log_dir = _resolve_log_dir()
        log_dir.mkdir(parents=True, exist_ok=True)
        # 按日期轮转:每天零点把 app.log 更名为 app.log.<当天日期>,保留 180 天。
        # 原实现按大小(RotatingFileHandler, 5MB×3),但实测 26 个使用日只用
        # 3.2MB、最大单日 686KB —— 5MB 阈值要 26 天才轮转一次,等于没轮,
        # 文件会跨月累积(这正是"不便查看"的成因)。
        #
        # utc 不传(默认 False,本机时间),与界面上看到的日志时间戳一致。
        #
        # ⚠️ TimedRotatingFileHandler 在多进程下会各自判断轮转时刻而重复轮转,
        # 但本项目 worker 的 handler 被 install_forwarding 清空换成队列转发,
        # 只有主进程写文件(见 log_forwarder.py 与 tests/test_log_forwarder.py)。
        fh = TimedRotatingFileHandler(
            log_dir / "app.log",
            when="midnight",
            backupCount=180,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass  # 文件不可用/不可写时仅保留内存与控制台
    # 控制台(与 uvicorn 同窗口可见)
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)
    logger.addHandler(_ring)
    logger._tdt_ready = True  # type: ignore[attr-defined]


_setup()


def set_notifier(cb) -> None:
    """注入 WS 广播回调:每条新日志实时推给前端。"""
    _ring.set_notifier(cb)


def recent_logs(limit: int = 300) -> list[dict]:
    """取最近的日志(供 GET /api/logs)。"""
    return _ring.recent(limit)
