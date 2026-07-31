"""FastAPI 入口:挂载 API、WebSocket、静态前端,启动任务队列 worker。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .core.logs import logger as app_logger, recent_logs, set_notifier as set_log_notifier
from .core.queue import task_queue
from .core.runner import run_task
from .core.token_pool import token_pool
from .db import init_db
from .models import reset_stale_running
from .paths import resource_root
from .api import buildings as buildings_api
from .api import local as local_api
from .api import tasks as tasks_api
from .api import tokens as tokens_api
from .api import ws as ws_api

# 只读资源根:开发时为项目根,打包后为 _MEIPASS 解包目录
RES_DIR = resource_root()
# 优先挂 Vue 版构建产物(frontendvue/dist);未构建时回退旧版纯静态 frontend
_VUE_DIST = RES_DIR / "frontendvue" / "dist"
FRONTEND_DIR = _VUE_DIST if (_VUE_DIST / "index.html").exists() else RES_DIR / "frontend"
# 预览页读取的静态资源:成果目录(可写,走 settings) + NaturalEarthII 离线底图(只读)
OUTPUT_DIR = settings.output_dir
BASEMAP_DIR = RES_DIR / "exmple-data" / "NaturalEarthII"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    reset_stale_running()
    task_queue.set_runner(run_task)
    task_queue.start()
    # tk 使用池:池空时把 config.yaml 的密钥作为初始种子导入;注入 WS 广播回调
    token_pool.seed_from_config(settings.tianditu.token)
    token_pool.reload()
    loop = asyncio.get_running_loop()

    def _token_notify(msg: dict, _loop=loop):
        asyncio.run_coroutine_threadsafe(task_queue.broadcast(msg), _loop)

    token_pool.set_notifier(_token_notify)
    # 运行日志实时推送给前端(经队列 WS 广播)
    set_log_notifier(_token_notify)
    app_logger.info("服务已启动,监听 %s:%s", settings.server.host, settings.server.port)
    try:
        yield
    finally:
        # 退出前把未落库的计数写回
        token_pool.flush()


app = FastAPI(title="天地图下载处理工具", lifespan=lifespan)

app.include_router(tasks_api.router)
app.include_router(tokens_api.router)
app.include_router(ws_api.router)
app.include_router(buildings_api.router)
app.include_router(local_api.router)


@app.get("/api/config")
async def api_config():
    """前端启动时读取:是否已配置密钥、输出目录等(不回传密钥明文)。"""
    return {
        "has_token": bool(settings.tianditu.token) or token_pool.has_any(),
        "has_pool_token": token_pool.has_any(),
        "output_dir": str(settings.output_dir),
        "concurrency": settings.download.concurrency,
        # 底图显示用密钥(前端加载天地图影像底图需要)
        "basemap_token": settings.tianditu.basemap_or_download(),
    }


@app.get("/api/capabilities")
async def api_capabilities():
    """按数据源列出可用的导出格式与容器格式。

    前端据此渲染格式勾选与容器下拉,不再各 tab 硬编码——新增格式只需改
    core.formats 注册表,界面自动跟上。
    """
    from .core.formats import (CONTAINERS, PIPE_BUILDING, PIPE_RASTER,
                               PROVIDER_KIND, DataKind, kind_of, stages_for)

    def stage_json(s):
        return {
            "key": s.key,
            "label": s.label,
            "default_on": s.default_on,
            "note": s.note,
            "containers": [
                {"key": c, "label": CONTAINERS[c].label,
                 "ext": CONTAINERS[c].ext, "note": CONTAINERS[c].note,
                 "sidecars": list(CONTAINERS[c].sidecars)}
                for c in s.containers if c in CONTAINERS
            ],
        }

    providers = {}
    for key in PROVIDER_KIND:
        kind = kind_of(key)
        pipe = (PIPE_BUILDING if kind == DataKind.VECTOR_POLYGON else PIPE_RASTER)
        providers[key] = {
            "kind": kind,
            "stages": [stage_json(s) for s in stages_for(kind, pipe)],
        }
    return {"providers": providers}


@app.get("/api/logs")
async def api_logs(limit: int = 300):
    """返回最近的运行日志(内存环形缓冲),供前端「日志」抽屉打开时拉取历史。"""
    return {"logs": recent_logs(limit)}


# 预览页静态资源(放在 catch-all / 之前,避免被前端兜底覆盖)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/output", StaticFiles(directory=str(OUTPUT_DIR)), name="output")
if BASEMAP_DIR.exists():
    app.mount("/basemap", StaticFiles(directory=str(BASEMAP_DIR)), name="basemap")

# 静态前端挂到根路径(放最后,避免覆盖 /api 与 /ws)
app.mount("/", StaticFiles(directory=str(FRONTEND_DIR), html=True), name="frontend")
