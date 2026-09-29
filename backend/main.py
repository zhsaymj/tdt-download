"""FastAPI 入口:挂载 API、WebSocket、静态前端,启动任务队列 worker。"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .config import settings
from .core.cache_migrate import migrate_cache_grids
from .core.logs import logger as app_logger, recent_logs, set_notifier as set_log_notifier
from .core.queue import task_queue
from .core.token_pool import token_pool
from .db import init_db
from .models import reset_stale_running
from .paths import resource_root
from .api import buildings as buildings_api
from .api import local as local_api
from .api import services as services_api
from .api import tasks as tasks_api
from .api import tiles as tiles_api
from .api import tokens as tokens_api
from .api import tools as tools_api
from .api import ws as ws_api

# 只读资源根:开发时为项目根,打包后为 _MEIPASS 解包目录
RES_DIR = resource_root()
# 挂 Vue 版构建产物(frontendvue/dist,已删除的旧版纯静态 frontend 不再回退)
FRONTEND_DIR = RES_DIR / "frontendvue" / "dist"
# 预览页读取的静态资源:成果目录(可写,走 settings) + NaturalEarthII 离线底图(只读)
OUTPUT_DIR = settings.output_dir
BASEMAP_DIR = RES_DIR / "exmple-data" / "NaturalEarthII"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    reset_stale_running()
    # 瓦片缓存目录带网格后缀的一次性迁移(设计 D2):`tianditu_img/` → `tianditu_img_c/`。
    # 不加后缀的话,同一个图层的 `_c`(geodetic)与 `_w`(mercator)两套行号不同的
    # 瓦片会互相覆盖、断点续传时静默错乱。实测现有天地图影像缓存 18 级 18.2 万张,
    # 不迁移等于白下。幂等:迁过再调返回 0。注记类 key 刻意不迁(见模块说明)。
    _moved = migrate_cache_grids(settings.cache_dir)
    if _moved:
        app_logger.info("缓存迁移:已为 %d 个目录加上网格后缀", _moved)
    # 主进程只做调度:任务由 worker 子进程执行,不再注册本地 runner
    task_queue.start()
    # 预览瓦片转发用的共享 HTTP session(必须在事件循环里创建)
    await tiles_api.startup()
    # tk 使用池:池空时把 config.yaml 的密钥作为初始种子导入;注入 WS 广播回调
    token_pool.seed_from_config(settings.tianditu.token)
    token_pool.reload()
    loop = asyncio.get_running_loop()

    def _token_notify(msg: dict, _loop=loop):
        asyncio.run_coroutine_threadsafe(task_queue.broadcast(msg), _loop)

    token_pool.set_notifier(_token_notify)
    # 运行日志实时推送给前端(经队列 WS 广播)
    set_log_notifier(_token_notify)
    app_logger.info("服务已启动,监听 %s:%s(worker=%s)",
                    settings.server.host, settings.server.port,
                    settings.worker.num_workers)
    try:
        yield
    finally:
        # 退出前把未落库的计数写回
        token_pool.flush()
        # 先关预览 session:下面 task_queue.shutdown() 会 join worker 数十秒,
        # 放它后面会让 session 悬着那么久(Unclosed client session 警告)
        await tiles_api.shutdown()
        # 收工:通知 worker 退出,超时强杀。
        # 必须 await —— shutdown 内部 join 每个 worker(最坏 timeout×N 秒),
        # 同步调用会阻塞事件循环,期间服务完全不响应、Ctrl-C 收尾也被拖着。
        await task_queue.shutdown()


app = FastAPI(title="天地图下载处理工具", lifespan=lifespan)

app.include_router(tasks_api.router)
app.include_router(tiles_api.router)
app.include_router(tokens_api.router)
app.include_router(ws_api.router)
app.include_router(buildings_api.router)
app.include_router(local_api.router)
app.include_router(tools_api.router)
app.include_router(services_api.router)


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
    from .core.formats import (CONTAINERS, PIPE_3D, PIPE_BUILDING, PIPE_RASTER,
                               PROVIDER_KIND, DataKind, default_on_stage_keys,
                               kind_of, stages_for)

    def stage_json(s, default_keys):
        return {
            "key": s.key,
            "label": s.label,
            # 默认勾选按**源自身的网格**定,不用注册表里静态的 default_on ——
            # 对墨卡托源出 geodetic TMS 要重投影、文字会发虚(需求37),
            # 详见 core.formats.default_on_stage_keys
            "default_on": s.key in default_keys,
            "note": s.note,
            "containers": [
                {"key": c, "label": CONTAINERS[c].label,
                 "ext": CONTAINERS[c].ext, "note": CONTAINERS[c].note,
                 "sidecars": list(CONTAINERS[c].sidecars)}
                for c in s.containers if c in CONTAINERS
            ],
        }

    # 数据类型 → 管线。三维阶段(pipeline="3d")若按 PIPE_RASTER 过滤会全部
    # 被排除,local_osgb/local_pointcloud 的 stages 变空、前端渲染不出格式勾选。
    kind_pipe = {
        DataKind.VECTOR_POLYGON: PIPE_BUILDING,
        DataKind.MESH_OSGB: PIPE_3D,
        DataKind.POINT_CLOUD: PIPE_3D,
    }
    providers = {}
    for key in PROVIDER_KIND:
        kind = kind_of(key)
        pipe = kind_pipe.get(kind, PIPE_RASTER)
        stages = stages_for(kind, pipe)
        default_keys = default_on_stage_keys(key, stages)
        providers[key] = {
            "kind": kind,
            "stages": [stage_json(s, default_keys) for s in stages],
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
