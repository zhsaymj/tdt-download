"""底图预览瓦片转发(Google / Esri World Imagery)。

为什么需要转发:浏览器无法使用后端的代理配置,而这两个源直连不通。

⚠️ **本端点跑在主进程**,而下载跑在 worker 子进程 —— 是两个进程。
这带来三条必须遵守的约束:

1. **独立限流**。下载器的 asyncio.Semaphore 在 worker 进程里,对本端点
   完全无效。两者共用同一个本机代理出口:下载峰值 num_workers ×
   download.concurrency(默认 2×8=16),OpenLayers 平移一次又能并发
   20+ 张。不独立限流会把代理打满。

2. **短超时**。download.timeout 默认 30s,这里绝不能用 —— 主进程的事件
   循环要服务所有 HTTP 与 WebSocket,20 个挂 30s 的转发请求会拖垮交互,
   那正是进程隔离改造要解决的问题,不该从这里漏回来。

3. **复用单个 session**。每请求新建 ClientSession 会重建连接池与代理隧道
   (https 要重新 CONNECT 握手),预览这种高频小请求下开销显著。

另外:**不写缓存**(设计 D6)。预览与下载用途不同(预览要即时、可失败;
下载要完整、可续传),混用缓存会让下载的进度估算失真。
"""
from __future__ import annotations

import asyncio

import aiohttp
from fastapi import APIRouter, HTTPException, Response

from ..config import settings
from ..core.logs import logger
from ..providers.esri_imagery import (build_esri_imagery_provider,
                                      is_esri_imagery_provider)
from ..providers.google import build_google_provider, is_google_provider

router = APIRouter(prefix="/api/tiles", tags=["tiles"])

#: 预览转发的并发上限。刻意小于 download.concurrency —— 预览是"看一眼",
#: 下载是"要完整成果";争抢代理时应当让下载优先。
#: 这个信号量是主进程模块级单例,与 worker 里下载器的信号量是两个独立的闸,
#: 二者之和才是代理的实际峰值压力(设计预算:16 + 4 = 20)。
_PREVIEW_CONCURRENCY = 4

#: 预览取瓦片超时(秒)。短:拿不到就返回占位图让地图继续可用,
#: 而不是让用户对着转圈的瓦片等 30 秒。
_PREVIEW_TIMEOUT = 8

_sem = asyncio.Semaphore(_PREVIEW_CONCURRENCY)
_session: aiohttp.ClientSession | None = None

#: 1x1 全透明 PNG。取不到瓦片时返回它,避免地图出现破图图标。
_TRANSPARENT_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
    0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00,
    0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
    0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
])


async def startup() -> None:
    """建预览用的共享 session(由 main.py 的 lifespan 调用)。

    必须在运行中的事件循环里创建 —— ClientSession 会绑定创建时的 loop,
    模块导入期(无 loop)创建会在首次使用时报
    "Timeout context manager should be used inside a task"。

    这里只统一超时,**不设 session 级 proxy**:不同 provider 可能配不同代理
    (google.proxy 与 esri_imagery.proxy 是两个字段),故 proxy 逐请求传。
    这与下载器"session 级 proxy"的选择相反,理由也相反:下载器一个 session
    只服务一个 provider(重试时必须一致),本端点一个 session 服务所有 provider。
    """
    global _session
    if _session is None:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_PREVIEW_TIMEOUT))


async def shutdown() -> None:
    """关闭 session。应在 task_queue.shutdown() **之前**调用 ——
    后者会 await to_thread 做数十秒的 join,放在它后面关会让 session
    悬着那么久(日志出现 Unclosed client session)。"""
    global _session
    if _session is not None:
        await _session.close()
        _session = None


def _resolve_preview_provider(provider: str):
    """把 provider key 解析为实例;不在白名单或未启用则抛 HTTP 错误。

    只接受已登记且 enabled 的 key —— 否则这个端点就成了任意 URL 代理
    (SSRF)。天地图不走这里:它由前端直连(有自己的 token 机制)。
    """
    if is_google_provider(provider):
        if not settings.google.enabled:
            raise HTTPException(
                403, "Google 影像未启用(config.yaml 中设 google.enabled: true)")
        return build_google_provider(provider, settings.google)
    if is_esri_imagery_provider(provider):
        if not settings.esri_imagery.enabled:
            raise HTTPException(
                403, "Esri World Imagery 未启用"
                     "(config.yaml 中设 esri_imagery.enabled: true)")
        return build_esri_imagery_provider(settings.esri_imagery)
    raise HTTPException(404, f"不支持预览的数据源:{provider}")


@router.get("/{provider}/{z}/{x}/{y}")
async def api_preview_tile(provider: str, z: int, x: int, y: int):
    """转发一张预览瓦片。失败时返回透明占位图,不破坏地图渲染。"""
    p = _resolve_preview_provider(provider)
    if _session is None:
        # lifespan 未跑(如单测直接调路由):降级为占位图而非 500
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")

    url = p.tile_url(x, y, z)
    try:
        async with _sem:
            async with _session.get(url, proxy=p.proxy) as resp:
                if resp.status != 200:
                    return Response(content=_TRANSPARENT_PNG,
                                    media_type="image/png")
                data = await resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        # 代理不通/超时:返回占位图。不记 error 级日志 —— 预览失败是常态
        # (代理波动、平移太快),刷屏的日志会盖掉真正的问题。
        logger.debug("预览瓦片取失败 %s z=%s x=%s y=%s:%s",
                     provider, z, x, y, e)
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")

    if not data:
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")
    # 浏览器侧缓存 1 小时:预览不写服务端缓存(设计 D6),但让浏览器缓存
    # 能显著减少平移时的重复请求与代理压力。
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=3600"})
