"""集中式运行日志:滚动文件 + 内存环形缓冲 + WebSocket 实时推送。

三处出口便于排查"是不是卡住了":
  1) 滚动文件 {ROOT}/data/logs/app.log(重启保留,可事后翻查)
  2) 内存环形缓冲(最近 N 条,供 GET /api/logs 拉取,页面打开即见历史)
  3) 注入的 notifier 回调(把每条日志经队列广播给前端,实时刷新)

用法:from .logs import logger; logger.info("...").
"""
from __future__ import annotations

import logging
from collections import deque
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler

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


def _setup() -> None:
    """初始化文件与环形处理器(幂等,重复调用只装一次)。"""
    if getattr(logger, "_tdt_ready", False):
        return
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    try:
        log_dir = ROOT / "data" / "logs"
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
        pass  # 文件不可写时仅保留内存/控制台
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
