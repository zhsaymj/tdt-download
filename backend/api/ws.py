"""WebSocket 进度推送。

前端连上 /ws/progress 后,订阅队列广播,实时收到 progress/task 消息。
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from ..core.queue import task_queue

router = APIRouter()


@router.websocket("/ws/progress")
async def ws_progress(ws: WebSocket):
    await ws.accept()

    async def send(msg: dict):
        await ws.send_json(msg)

    task_queue.subscribe(send)
    try:
        while True:
            # 保持连接;前端无需发消息,这里仅用于检测断开
            await asyncio.sleep(30)
            await ws.send_json({"type": "ping"})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        task_queue.unsubscribe(send)
