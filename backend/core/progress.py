"""阶段化进度跟踪器 StageTracker。

一个任务被拆成有序阶段(下载/合并/切片…),每个阶段独立跟踪
状态、百分比、剩余时间(ETA)。StageTracker 负责:
  - 维护 stages 列表(与 DB 存储结构一致)
  - 各阶段生命周期:start / update / finish / fail / pause / skip
  - 基于已用时与已完成量估算单阶段 ETA
  - 汇总任务总剩余时间(运行中阶段精确,待运行阶段按同类均速粗估)
  - 0.5s 限流地落库 + emit `stage` 广播消息

emit(msg: dict) 由队列注入,把消息同步广播给 WebSocket 订阅者。
"""
from __future__ import annotations

import time

from ..models import update_stages
from .logs import logger


class StageTracker:
    # ETA 近期速率窗口(秒):用最近这段时间的样本估斜率。窗口太短时,进度增量小的
    # 阶段(每次只涨 1-2)算出的速率会剧烈抖动、ETA 大幅跳变;取 30s 兼顾平滑与响应。
    _ETA_WINDOW = 30.0
    # 周期性进度日志间隔(秒):切片/合并中途每隔这么久打一条明细,避免刷屏
    _LOG_INTERVAL = 5.0

    def __init__(self, task_id: str, stages: list[dict], emit, task_name: str = ""):
        self.task_id = task_id
        self.task_name = task_name or task_id
        self.stages = stages
        self.emit = emit
        self._last_push = 0.0
        # 周期进度日志的上次落点:key -> monotonic 秒
        self._last_log: dict[str, float] = {}
        # 各阶段起始时间戳:key -> monotonic 秒
        self._started: dict[str, float] = {}
        # 各阶段进度采样:key -> [(monotonic, done), ...](用于近期速率 ETA)
        self._samples: dict[str, list[tuple[float, int]]] = {}

    # ---------- 查询 ----------

    def get(self, key: str) -> dict | None:
        for s in self.stages:
            if s["key"] == key:
                return s
        return None

    def has(self, key: str) -> bool:
        return self.get(key) is not None

    def is_done(self, key: str) -> bool:
        s = self.get(key)
        return bool(s and s["status"] in ("done", "skipped"))

    def pending_keys(self) -> list[str]:
        """所有尚未完成(非 done/skipped)的阶段 key,按顺序。"""
        return [s["key"] for s in self.stages if s["status"] not in ("done", "skipped")]

    # ---------- 生命周期 ----------

    def start(self, key: str, total: int = 0, message: str = "") -> None:
        s = self.get(key)
        if not s:
            return
        s["status"] = "running"
        s["done"] = 0
        s["total"] = total
        s["percent"] = 0.0
        s["eta_sec"] = None
        s["message"] = message
        now = time.monotonic()
        self._started[key] = now
        self._last_log[key] = now
        self._samples[key] = [(now, 0)]
        self._flush(force=True)

    def update(self, key: str, done: int | None = None, total: int | None = None,
               message: str | None = None) -> None:
        """更新运行中阶段的进度。done/total 为绝对值,message 可选。"""
        s = self.get(key)
        if not s:
            return
        if total is not None:
            s["total"] = total
        if done is not None:
            s["done"] = done
        if message is not None:
            s["message"] = message
        tot = s["total"] or 0
        dn = s["done"] or 0
        s["percent"] = round(min(dn / tot * 100, 100.0), 2) if tot else 0.0
        # ETA:用最近 _ETA_WINDOW 秒的样本算"近期速率",再外推剩余量。
        # 比全程平均更贴合当前快慢,且会随时间自然递减(不会冻结)。
        now = time.monotonic()
        if tot > 0 and dn > 0:
            samples = self._samples.setdefault(key, [])
            samples.append((now, dn))
            # 丢弃窗口外的旧样本(至少保留 2 个用于算斜率)
            cutoff = now - self._ETA_WINDOW
            while len(samples) > 2 and samples[0][0] < cutoff:
                samples.pop(0)
            t0, d0 = samples[0]
            dt = now - t0
            dd = dn - d0
            if dt > 0 and dd > 0:
                rate = dd / dt
                s["eta_sec"] = round(max(tot - dn, 0) / rate, 1)
            elif s.get("eta_sec") is None:
                # 样本还不足以算斜率:退回全程平均给个初值
                elapsed = now - self._started.get(key, now)
                if elapsed > 0:
                    s["eta_sec"] = round(max(tot - dn, 0) / (dn / elapsed), 1)
        self._flush()
        self._maybe_log_progress(key)

    def finish(self, key: str, message: str = "") -> None:
        s = self.get(key)
        if not s:
            return
        s["status"] = "done"
        s["percent"] = 100.0
        s["eta_sec"] = 0
        if message:
            s["message"] = message
        self._samples.pop(key, None)
        self._flush(force=True)

    def fail(self, key: str, message: str = "") -> None:
        s = self.get(key)
        if not s:
            return
        s["status"] = "failed"
        s["eta_sec"] = None
        if message:
            s["message"] = message
        self._flush(force=True)

    def pause(self, key: str, message: str = "") -> None:
        s = self.get(key)
        if not s:
            return
        s["status"] = "paused"
        s["eta_sec"] = None
        if message:
            s["message"] = message
        self._flush(force=True)

    def skip(self, key: str, message: str = "") -> None:
        s = self.get(key)
        if not s:
            return
        s["status"] = "skipped"
        s["percent"] = 100.0
        s["eta_sec"] = 0
        if message:
            s["message"] = message
        self._flush(force=True)

    # ---------- 汇总与推送 ----------

    # 待运行阶段的粗估基准:导出阶段(合并/切片,本地 CPU/IO)通常远快于
    # 下载(受网络限制)。无历史可参考时,用当前"下载"阶段的预计总耗时乘此
    # 系数作为每个待运行导出阶段的粗估。经验值,仅用于总剩余的量级估计。
    _EXPORT_TIME_RATIO = 0.35

    def total_eta(self) -> float | None:
        """任务总剩余秒数:运行中阶段用精确 ETA,待运行阶段粗估。

        待运行阶段的单阶段耗时参考(按优先级):
          1) 已完成阶段的实测耗时均值(最准);
          2) 当前运行阶段的"预计单阶段总耗时"(elapsed + eta)——若当前是下载
             阶段,乘 _EXPORT_TIME_RATIO(导出比下载快);同为导出则约 1:1。
        这样即使还没有阶段完成,后续待运行阶段也会计入总剩余,不再被漏算。
        """
        now = time.monotonic()
        total = 0.0
        any_known = False

        # 参考 1:已完成阶段实测耗时均值
        finished_durs = []
        for s in self.stages:
            started = self._started.get(s["key"])
            if s["status"] == "done" and started is not None:
                finished_durs.append(max(now - started, 0))
        avg_done = sum(finished_durs) / len(finished_durs) if finished_durs else None

        # 参考 2:当前运行阶段的预计单阶段总耗时(elapsed + eta),及它是不是下载
        running_total = None
        running_is_download = False
        for s in self.stages:
            if s["status"] == "running" and s.get("eta_sec") is not None:
                started = self._started.get(s["key"], now)
                running_total = max(now - started, 0) + s["eta_sec"]
                running_is_download = (s["key"] == "download")
                break

        # 待运行阶段的单阶段耗时估计
        if avg_done is not None:
            pending_est = avg_done
        elif running_total is not None:
            pending_est = running_total * (self._EXPORT_TIME_RATIO
                                           if running_is_download else 1.0)
        else:
            pending_est = None

        for s in self.stages:
            st = s["status"]
            if st in ("done", "skipped"):
                continue
            if st == "running" and s.get("eta_sec") is not None:
                total += s["eta_sec"]
                any_known = True
            elif pending_est is not None:   # pending/paused/failed/无 eta 的 running
                total += pending_est
                any_known = True
        return round(total, 1) if any_known else None

    def snapshot(self) -> dict:
        """构造一条 stage 广播消息(含全部阶段与总剩余时间)。"""
        return {
            "type": "stage", "id": self.task_id,
            "stages": self.stages, "total_eta_sec": self.total_eta(),
        }

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_push < 0.5:
            return
        self._last_push = now
        update_stages(self.task_id, self.stages)
        self.emit(self.snapshot())

    def _fmt_eta(self, eta) -> str:
        """把剩余秒数格式化成可读文本(与前端一致的粗粒度)。"""
        if eta is None:
            return "—"
        eta = int(eta)
        if eta < 60:
            return f"{eta}秒"
        if eta < 3600:
            return f"{eta // 60}分{eta % 60}秒"
        return f"{eta // 3600}时{(eta % 3600) // 60}分"

    def _maybe_log_progress(self, key: str) -> None:
        """运行中阶段每隔 _LOG_INTERVAL 秒打一条进度明细(含速率/剩余时间)。

        由 update 调用;按阶段独立限流,避免几万张瓦片逐张刷屏。
        """
        s = self.get(key)
        if not s or s["status"] != "running":
            return
        now = time.monotonic()
        last = self._last_log.get(key, 0.0)
        if now - last < self._LOG_INTERVAL:
            return
        self._last_log[key] = now
        dn = s.get("done") or 0
        tot = s.get("total") or 0
        # 近期速率:用样本窗口首尾算。快时显示"N/秒",慢时(<1/秒)显示"N 秒/个"
        # 避免四舍五入成 0/秒(慢阶段看着像卡死)。
        rate_txt = ""
        samples = self._samples.get(key) or []
        if len(samples) >= 2:
            t0, d0 = samples[0]
            dt = now - t0
            dd = dn - d0
            if dt > 0 and dd > 0:
                rate = dd / dt
                rate_txt = (f",速率 {rate:.1f}/秒" if rate >= 1
                            else f",速率 {dt / dd:.1f}秒/个")
        label = s.get("label", key)
        logger.info("任务[%s] 阶段[%s] 进度 %d/%d(%.1f%%),剩余 %s%s",
                    self.task_name, label, dn, tot, s.get("percent") or 0.0,
                    self._fmt_eta(s.get("eta_sec")), rate_txt)
