"""跨进程协议消息定义:主进程 ↔ worker 进程。

Control 消息(主 → worker):通过 control_queue 投递。
Event 消息(worker → 主):通过 event_queue 回传。
"""
from __future__ import annotations


class ControlMessage:
    """主进程向 worker 下达的控制指令。"""

    @staticmethod
    def run(task_id: str) -> dict:
        """执行任务。"""
        return {"kind": "run", "task_id": task_id}

    @staticmethod
    def pause(task_id: str) -> dict:
        """暂停任务(协作式)。"""
        return {"kind": "pause", "task_id": task_id}

    @staticmethod
    def cancel(task_id: str) -> dict:
        """取消任务(协作式)。"""
        return {"kind": "cancel", "task_id": task_id}

    @staticmethod
    def shutdown() -> dict:
        """通知 worker 退出。"""
        return {"kind": "shutdown"}


class EventMessage:
    """worker 向主进程上报的事件。"""

    @staticmethod
    def event(payload: dict) -> dict:
        """进度/状态事件(原 emit 的消息体)。"""
        return {"kind": "event", "payload": payload}

    @staticmethod
    def log(level: str, msg: str, ts: float, worker_id: str = "") -> dict:
        """日志转发。

        worker_id 用于多 worker 时区分日志来源,默认空串以兼容单 worker 场景。
        """
        return {"kind": "log", "level": level, "msg": msg, "ts": ts,
                "worker_id": worker_id}

    @staticmethod
    def finished(task_id: str) -> dict:
        """任务结束(正常 / 异常 / 被取消)。"""
        return {"kind": "finished", "task_id": task_id}


def parse_control(msg: dict) -> tuple[str, dict]:
    """解析控制消息,返回 (kind, 剩余字段)。"""
    kind = msg.get("kind")
    if kind is None:
        raise ValueError("Missing 'kind' field in control message")
    if kind not in ("run", "pause", "cancel", "shutdown"):
        raise ValueError(f"Unknown control kind: {kind}")
    return kind, {k: v for k, v in msg.items() if k != "kind"}


def parse_event(msg: dict) -> tuple[str, dict]:
    """解析事件消息,返回 (kind, 剩余字段)。"""
    kind = msg.get("kind")
    if kind is None:
        raise ValueError("Missing 'kind' field in event message")
    if kind not in ("event", "log", "finished"):
        raise ValueError(f"Unknown event kind: {kind}")
    return kind, {k: v for k, v in msg.items() if k != "kind"}
