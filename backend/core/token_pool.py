"""天地图 tk 使用池:多密钥按顺序轮询,满额自动切换,全池用尽循环。

计数只统计后端下载器真正发出的瓦片请求(缓存命中/前端底图不计)。
每个 tk 有每日请求上限(默认 150 万),按自然日还原;跨天自动归零。
全池用满时通过 notifier 广播提示,并重置全池计数从第一个 tk 重新循环。

计数持久化到 SQLite tokens 表,批量落库(避免几十万次逐条写库)。
"""
from __future__ import annotations

import threading
from datetime import date, datetime

from ..db import get_conn

# 单 tk 每日默认请求上限
DEFAULT_MAX_REQUESTS = 1_500_000
# 累计多少次内存计数后落库一次(减少写库频次)
_FLUSH_EVERY = 1000


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _today() -> str:
    return date.today().isoformat()


# ---------- tokens 表 CRUD ----------

def list_tokens() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tokens ORDER BY order_index ASC, id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_token(token_id: int) -> dict | None:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tokens WHERE id=?", (token_id,)).fetchone()
    return dict(row) if row else None


def add_token(token: str, label: str = "", max_requests: int = DEFAULT_MAX_REQUESTS) -> int:
    token = (token or "").strip()
    if not token:
        raise ValueError("密钥不能为空")
    now = _now()
    with get_conn() as conn:
        # 追加到末尾:order_index = 当前最大值 + 1
        row = conn.execute("SELECT COALESCE(MAX(order_index), -1) AS m FROM tokens").fetchone()
        next_order = int(row["m"]) + 1
        cur = conn.execute(
            """INSERT INTO tokens
               (token, label, order_index, request_count, count_date,
                max_requests, enabled, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (token, label or "", next_order, 0, _today(),
             int(max_requests) if max_requests else DEFAULT_MAX_REQUESTS,
             1, now, now),
        )
        return int(cur.lastrowid)


def update_token(token_id: int, **fields) -> None:
    allowed = {"token", "label", "max_requests", "enabled", "order_index",
               "request_count", "count_date"}
    fields = {k: v for k, v in fields.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(f"UPDATE tokens SET {cols} WHERE id=?", (*fields.values(), token_id))


def delete_token(token_id: int) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tokens WHERE id=?", (token_id,))


def reorder_tokens(ordered_ids: list[int]) -> None:
    """按给定的 id 顺序重排 order_index。"""
    now = _now()
    with get_conn() as conn:
        for idx, tid in enumerate(ordered_ids):
            conn.execute(
                "UPDATE tokens SET order_index=?, updated_at=? WHERE id=?",
                (idx, now, tid),
            )


def reset_token_count(token_id: int) -> None:
    with get_conn() as conn:
        conn.execute(
            "UPDATE tokens SET request_count=0, count_date=?, updated_at=? WHERE id=?",
            (_today(), _now(), token_id),
        )


# ---------- 使用池单例 ----------

class TokenPool:
    """tk 使用池:线程安全地取当前密钥并计数。

    下载线程可能来自 aiohttp 事件循环的不同回调,故用锁保护内存状态。
    内存里维护每个 tk 的当日计数,达到上限自动切下一个;全部用尽则广播
    提示并重置全池计数从头循环。计数按 _FLUSH_EVERY 批量落库。
    """

    def __init__(self):
        self._lock = threading.RLock()
        # 内存镜像:[{id, token, label, max_requests, enabled, count, dirty}]
        self._items: list[dict] = []
        self._cursor = 0          # 当前使用的 tk 在 _items 中的下标
        self._day = _today()      # 内存计数所属自然日
        self._pending = 0         # 距上次落库累计的计数(达到阈值触发 flush)
        self._notifier = None     # WS 广播回调 notify(dict)
        self._exhausted = False   # 是否已处于"全池用尽"提示态(避免重复广播)

    # ----- 生命周期 -----

    def set_notifier(self, cb) -> None:
        self._notifier = cb

    def reload(self) -> None:
        """从数据库重建内存镜像(增删改 tk 后调用)。"""
        with self._lock:
            self.flush()  # 先把未落库的计数写回
            rows = list_tokens()
            today = _today()
            items = []
            for r in rows:
                # 跨天:当日计数还原为 0
                count = r["request_count"] if r.get("count_date") == today else 0
                items.append({
                    "id": r["id"], "token": r["token"], "label": r.get("label", ""),
                    "max_requests": r["max_requests"] or DEFAULT_MAX_REQUESTS,
                    "enabled": bool(r["enabled"]), "count": count, "dirty": False,
                })
            self._items = items
            self._day = today
            if self._cursor >= len(items):
                self._cursor = 0
            self._exhausted = False

    def seed_from_config(self, token: str) -> None:
        """池为空且 config 有 token 时,把它作为第一个 tk 种子导入。"""
        token = (token or "").strip()
        if not token:
            return
        if list_tokens():
            return
        try:
            add_token(token, label="config.yaml 初始密钥")
        except ValueError:
            pass
        self.reload()

    def _notify(self, msg: dict) -> None:
        if self._notifier:
            try:
                self._notifier(msg)
            except Exception:
                pass

    def _rollover_day_if_needed(self) -> None:
        """跨自然日:全池当日计数还原为 0。调用方须持锁。"""
        today = _today()
        if today != self._day:
            for it in self._items:
                it["count"] = 0
                it["dirty"] = True
            self._day = today
            self._exhausted = False
            self._cursor = 0

    def _enabled_indices(self) -> list[int]:
        return [i for i, it in enumerate(self._items) if it["enabled"]]

    def _advance_to_available(self) -> int | None:
        """从当前游标起找一个未达上限的启用 tk。调用方须持锁。

        全部用尽时:广播提示 → 重置全池计数 → 从第一个可用 tk 重新开始。
        返回可用下标;池中无任何启用 tk 时返回 None。
        """
        enabled = self._enabled_indices()
        if not enabled:
            return None
        # 游标若落在停用项上,归位到第一个启用项
        if self._cursor not in enabled:
            self._cursor = enabled[0]
        # 从当前游标沿启用序列找未满的
        start_pos = enabled.index(self._cursor)
        for k in range(len(enabled)):
            idx = enabled[(start_pos + k) % len(enabled)]
            it = self._items[idx]
            if it["count"] < it["max_requests"]:
                if idx != self._cursor:
                    self._cursor = idx
                    self._notify({
                        "type": "token", "event": "switch",
                        "id": it["id"], "label": it["label"],
                        "message": f"密钥已切换到「{it['label'] or ('#' + str(it['id']))}」",
                    })
                self._exhausted = False
                return idx
        # 全部用尽:提示 + 重置全池计数从头循环
        if not self._exhausted:
            self._notify({
                "type": "token", "event": "exhausted",
                "message": "池中所有密钥当日配额均已用尽,已自动重置计数从第一个密钥重新循环使用。",
            })
        for it in self._items:
            it["count"] = 0
            it["dirty"] = True
        self._exhausted = True
        self._cursor = enabled[0]
        return enabled[0]

    def use_token(self) -> str:
        """取当前密钥并计数 +1(供 provider 每次生成瓦片 URL 时调用)。

        池为空时抛 RuntimeError(上层校验应已阻止走到这)。
        """
        with self._lock:
            if not self._items:
                self.reload()
            if not self._items:
                raise RuntimeError("密钥池为空,请先在密钥管理中添加天地图密钥。")
            self._rollover_day_if_needed()
            idx = self._advance_to_available()
            if idx is None:
                raise RuntimeError("密钥池中没有启用的密钥,请在密钥管理中启用或添加。")
            it = self._items[idx]
            it["count"] += 1
            it["dirty"] = True
            self._pending += 1
            if self._pending >= _FLUSH_EVERY:
                self._flush_locked()
            return it["token"]

    def current_token(self) -> str | None:
        """不计数地取一个当前可用密钥(如注记 provider 预取);池空返回 None。"""
        with self._lock:
            if not self._items:
                self.reload()
            if not self._items:
                return None
            self._rollover_day_if_needed()
            idx = self._advance_to_available()
            return self._items[idx]["token"] if idx is not None else None

    def has_available(self) -> bool:
        with self._lock:
            if not self._items:
                self.reload()
            self._rollover_day_if_needed()
            return any(
                it["enabled"] and it["count"] < it["max_requests"]
                for it in self._items
            )

    def has_any(self) -> bool:
        with self._lock:
            if not self._items:
                self.reload()
            return bool(self._items)

    # ----- 落库 -----

    def _flush_locked(self) -> None:
        """把内存里 dirty 的计数写回数据库。调用方须持锁。"""
        dirty = [it for it in self._items if it["dirty"]]
        if not dirty:
            self._pending = 0
            return
        now = _now()
        with get_conn() as conn:
            for it in dirty:
                conn.execute(
                    "UPDATE tokens SET request_count=?, count_date=?, updated_at=? WHERE id=?",
                    (it["count"], self._day, now, it["id"]),
                )
                it["dirty"] = False
        self._pending = 0

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _current_id_readonly(self) -> int | None:
        """只读地推算当前应使用的 tk id(跳过停用/已满项,不改游标、不广播)。"""
        enabled = self._enabled_indices()
        if not enabled:
            return None
        start = self._cursor if self._cursor in enabled else enabled[0]
        start_pos = enabled.index(start)
        for k in range(len(enabled)):
            idx = enabled[(start_pos + k) % len(enabled)]
            it = self._items[idx]
            if it["count"] < it["max_requests"]:
                return it["id"]
        # 全部已满(下次 use_token 会重置循环):当前指向第一个启用项
        return self._items[enabled[0]]["id"]

    def status(self) -> list[dict]:
        """返回当前池的运行态(供 API 展示):内存计数优先于库值。"""
        with self._lock:
            if not self._items:
                self.reload()
            self._rollover_day_if_needed()
            cur_id = self._current_id_readonly()
            out = []
            for it in self._items:
                out.append({
                    "id": it["id"], "label": it["label"],
                    "request_count": it["count"], "max_requests": it["max_requests"],
                    "enabled": it["enabled"], "is_current": it["id"] == cur_id,
                })
            return out


# 全局单例
token_pool = TokenPool()
