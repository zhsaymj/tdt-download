"""任务相关 REST API。"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..config import settings
from ..core.dem_tiling import (
    TILE_SIZE as DEM_TILE_SIZE, estimate_dem_tiles, mercator_range_for_bbox,
)
from ..core.formats import GEO_MERCATOR, grid_of
from ..core.mercator_tiling import estimate_mercator_tiles, suggest_mercator_levels
from ..core.logs import logger
from ..core.queue import task_queue
from ..core.tiling import estimate_levels, estimate_levels_detail, estimate_total_tiles
from ..core.token_pool import token_pool
from ..models import (
    TaskCreate, build_stage_defs, create_task, delete_task, get_task,
    list_tasks, normalize_tms_source_strategy, parse_export, update_task,
)
from ..providers.buildings import is_building_provider
from ..providers.esri_imagery import (build_esri_imagery_provider,
                                      is_esri_imagery_provider)
from ..providers.google import build_google_provider, is_google_provider
from ..providers.terrain import DEM_LAYERS, is_dem_provider


def _grids_for_estimate(provider: str, formats) -> list[str]:
    """该任务实际要下载哪些网格(与下载阶段同一个判定,见 core.formats)。"""
    from ..core.formats import download_grids_of
    if isinstance(formats, str):
        formats = parse_export(formats)
    return download_grids_of(provider, formats or [])


def _estimate_total(bbox, levels: list[int], provider: str, formats=()) -> int:
    """按**实际要下载的网格**累计瓦片数。

    天地图同时勾 tms+osm 时会下两套原生瓦片,而两套的计数不同(geodetic 第 z 级
    是 2^z×2^(z-1)、mercator 是 2^z×2^z)—— **不是翻倍**,得各算一遍再相加。
    少算一半会让用户以为实际下载量出了 bug。

    刻意不在这里做任何上限检查:用户明确要求不设单任务瓦片数上限(设计 §9 Q1),
    规模由调用方如实呈现、由用户判断。
    """
    return sum(_estimate_grid_total(bbox, levels, provider, g)
               for g in _grids_for_estimate(provider, formats))


def _estimate_grid_total(bbox, levels: list[int], provider: str, grid: str) -> int:
    """单个网格的瓦片数。墨卡托(Google/Esri/DEM)用 XYZ 计数,geodetic 用 4326。"""
    if grid == GEO_MERCATOR:
        return estimate_mercator_tiles(bbox, levels)
    return estimate_levels(bbox, levels)


def _z_cap_for(provider: str) -> int:
    """该数据源的服务级最高级别。

    委托给 core.formats(级别范围的唯一判定处)—— 此处曾是第二份实现,
    与 models.level_list 的硬编码 18 矛盾,导致 Google z21 / Esri z19 下不到。
    """
    from ..core.formats import z_cap_of
    return z_cap_of(provider)


def _z_floor_for(provider: str) -> int:
    """该数据源的最低级别。委托给 core.formats(同上前提)。"""
    from ..core.formats import z_floor_of
    return z_floor_of(provider)


# DEM 单瓦片平均字节数(Esri Terrain3D LERC 经验值,约 40-90KB)
_DEM_AVG_BYTES = 60 * 1024

# 墨卡托影像单瓦片平均字节数。实测 Google/Esri 的 jpg 在 9~27KB 之间,
# 取 18KB 作估算基准(设计 §3.8)。比 DEM 的 LERC 小得多,不能套用后者。
_MERC_IMG_AVG_BYTES = 18 * 1024


def _estimate_detail(bbox, levels: list[int], provider: str, formats=()) -> dict:
    """预估明细,按实际要下载的网格累计(见 _estimate_total)。

    ⚠️ 取消瓦片数硬上限后(设计 §9 Q1),**这是用户提交前唯一能看到规模的
    途径** —— 3 度选区 z21 是 5.3 亿张瓦片、约 9.6 TB。如实返回,不拦截。
    """
    grids = _grids_for_estimate(provider, formats)
    if len(grids) == 1:
        # 单网格:返回值形状与改动前**完全一致**(不塞额外键 —— 有测试做严格相等)
        return _estimate_grid_detail(bbox, levels, provider, grids[0])

    # 双网格:两套各算一遍,逐级把瓦片数与字节数**相加**(两套计数不同,不能翻倍)。
    # ⚠️ cols/rows/width/height 是几何量,**不合并** —— 取主网格(第一项,即源自己
    # 的网格)的值,界面上"总尺寸"按它显示;两套网格的几何尺寸本就不同,相加无意义。
    parts = [_estimate_grid_detail(bbox, levels, provider, g) for g in grids]
    per = []
    for i, row in enumerate(parts[0]["levels"]):
        merged = dict(row)
        merged["tiles"] = sum(p["levels"][i]["tiles"] for p in parts)
        merged["bytes"] = sum(p["levels"][i]["bytes"] for p in parts)
        per.append(merged)
    return {
        "levels": per,
        "total_tiles": sum(p["total_tiles"] for p in parts),
        "total_bytes": sum(p["total_bytes"] for p in parts),
    }


def _estimate_grid_detail(bbox, levels: list[int], provider: str,
                          grid: str) -> dict:
    """单个网格的预估明细。"""
    if grid != GEO_MERCATOR:
        return estimate_levels_detail(bbox, levels, provider)
    avg = _DEM_AVG_BYTES if is_dem_provider(provider) else _MERC_IMG_AVG_BYTES
    w, s, e, n = bbox
    per = []
    total_tiles = 0
    for z in sorted(set(levels)):
        tr = mercator_range_for_bbox(w, s, e, n, z)
        tiles = tr.count
        per.append({"z": z, "tiles": tiles, "bytes": tiles * avg,
                    "cols": tr.cols, "rows": tr.rows,
                    "width": tr.cols * DEM_TILE_SIZE,
                    "height": tr.rows * DEM_TILE_SIZE})
        total_tiles += tiles
    return {"levels": per, "total_tiles": total_tiles,
            "total_bytes": total_tiles * avg}


def _needs_proxy_provider(provider: str) -> bool:
    """该数据源是否需要代理才能取瓦片。

    实测:Google 与 Esri World Imagery 直连均不通;天地图与 Esri Terrain3D
    直连可用,本地文件源不联网。故只有前两者走预检。
    """
    return is_google_provider(provider) or is_esri_imagery_provider(provider)


def _proxy_config_key(provider: str) -> str:
    """该数据源的代理配置项名(用于错误文案,让用户知道改哪里)。"""
    if is_google_provider(provider):
        return "google.proxy"
    if is_esri_imagery_provider(provider):
        return "esri_imagery.proxy"
    return ""


def _is_connection_refused(exc: BaseException) -> bool:
    """异常链里是否含"连接被拒绝"。

    这是**代理没开**的确证信号:端口无监听时系统会立刻拒绝连接
    (实测 WinError 10061,异常链为 URLError -> ConnectionRefusedError),
    与"超时"有本质区别 —— 超时可能只是网络抖动,不能据此拒绝提交。

    必须沿异常链找:urllib 把底层异常包在 URLError 里(URLError.reason)。
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ConnectionRefusedError):
            return True
        reason = getattr(cur, "reason", None)
        if isinstance(reason, BaseException):
            if isinstance(reason, ConnectionRefusedError):
                return True
            cur = reason
            continue
        cur = cur.__cause__ or cur.__context__
    return False


def _probe_provider_reachable(provider: str, timeout: int = 6) -> bool | None:
    """取一张低级别瓦片判连通性。

    返回 True=通,False=不通(**确证**),None=判不了(超时等,**放行**)。

    取真实瓦片而非 HEAD 根域名:端点失效(旧 khms 端点 404)与代理不通是两种
    不同故障,只有真正请求一张瓦片才能区分 —— 而这正是用户最需要区分的两种
    情况(前者改 url_template,后者开代理)。

    用 z2 的瓦片:世界级,任何时候都该有数据,不受选区位置影响。
    """
    import urllib.error
    import urllib.request

    if is_google_provider(provider):
        p = build_google_provider(provider, settings.google)
    elif is_esri_imagery_provider(provider):
        p = build_esri_imagery_provider(settings.esri_imagery)
    else:
        return True

    url = p.tile_url(1, 1, 2)
    proxy = p.proxy
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy,
                                                   "https": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=p.headers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200 and bool(resp.read(64))
    except urllib.error.HTTPError:
        # 明确的 HTTP 错误(404/403):端点问题,不是代理不通
        return False
    except Exception as e:
        if _is_connection_refused(e):
            # 代理端口拒绝连接 = 代理软件没开(或端口写错)。确证,拒绝提交。
            #
            # 这一条不能省:代理没开恰恰是本预检存在的理由,而它走的正是
            # "连不上"这条分支 —— 若这里也返回 None(放行),预检就永远不会
            # 拦住任何东西,成了个看着有的空壳。
            return False
        # 超时等:区分不了"网络抖动"与"代理慢",按放行处理
        return None


async def _precheck_network_provider(provider: str) -> tuple[bool, str]:
    """提交前预检:该数据源现在能不能取到瓦片。返回 (是否放行, 错误文案)。

    为什么在主进程做:留给 worker 的话,代理不通表现为"任务变 running →
    瓦片逐张重试 → 全部失败",用户看到进度条停在 0% 然后红字失败,真正原因
    (代理没开)埋在日志里,还白占一个 worker 槽位数分钟。

    **判不了时放行**:与 probe_max_level 返回 None 时的取舍一致
    (providers/terrain.py:129)。不要把网络抖动当成"代理不通"而拒绝提交。
    """
    if not _needs_proxy_provider(provider):
        return True, ""
    reachable = await asyncio.to_thread(_probe_provider_reachable, provider)
    if reachable is not False:
        return True, ""           # True 或 None 都放行
    key = _proxy_config_key(provider)
    proxy = (settings.google.proxy if is_google_provider(provider)
             else settings.esri_imagery.proxy)
    where = f"代理 {proxy}" if proxy else "直连(未配置代理)"
    return False, (
        f"该数据源当前取不到瓦片({where})。"
        f"这两个源实测直连不通,必须配置 HTTP 代理:"
        f"请确认代理软件已启动,并检查 config.yaml 的 {key}。"
        f"注意修改配置后需**重启服务**才会生效。"
        f"若代理正常,则可能是端点已变更,请检查对应的 url_template。")


def _trim_levels_for_region(levels: list[int],
                            max_level: int | None) -> tuple[list[int], list[int]]:
    """按区域实际最高级别剔除超限级别。返回 (保留, 被剔除)。

    **剔除而非拒绝**:用户在西藏选了 z15-z18,z18 无数据时下 z15-z17 是
    合理的期望,不该整个任务被拒。被剔除的级别要回报给用户(任务详情里
    记明"已跳过 z18(该区域最高 z17)"),否则就是静默丢功能。

    max_level 为 None(判不了)时保留全部 —— 与 probe_max_level 的返回值
    语义一致,不把网络问题当成"该范围没影像"。
    """
    uniq = sorted(set(int(z) for z in levels))
    if max_level is None or not uniq:
        return uniq, []
    kept = [z for z in uniq if z <= max_level]
    dropped = [z for z in uniq if z > max_level]
    if not kept:
        # 全部超限:保留最低一级,避免产出零级别的空任务
        kept = [uniq[0]]
        dropped = uniq[1:]
    return kept, dropped


async def _probe_imagery_max_level(bbox, provider: str) -> int | None:
    """探测影像数据源在该范围的最高可用级别;None 表示判不了或不适用。

    只有 Esri 需要:实测 Google 陆地处处可到 z21,无地区性降级。
    """
    if not is_esri_imagery_provider(provider):
        return None
    if not settings.esri_imagery.probe_max_zoom:
        return None
    from ..providers.esri_imagery import probe_max_level
    # 逐级网络请求,必须放线程里(与 _probe_dem_max_level 一致)
    return await asyncio.to_thread(probe_max_level, bbox,
                                   settings.esri_imagery)



def _add_annotation_to_detail(detail: dict, levels: list[int]) -> None:
    """把注记瓦片计入预估明细(**原地修改** detail)。

    注记与底图共用同一个瓦片区间,所以逐级别"翻倍";但天地图注记最高
    z18,超过的级别一张都不下 —— 不是整体 ×2。

    逐级别与合计一起更新,避免界面表格与合计自相矛盾。

    与 api_create_task 用的是同一份逻辑(后者也走这个函数),两者不可能
    再漂移 —— 此前预估接口不接受 annotate、前端自己乘 2,结果
    z19~21 勾注记时预估是任务数的两倍。
    """
    from ..core.runner import ANNOTATION_MAX_Z

    if not detail.get("levels"):
        return
    for row in detail["levels"]:
        if row.get("z") is not None and row["z"] <= ANNOTATION_MAX_Z:
            row["tiles"] = row.get("tiles", 0) * 2
            row["bytes"] = row.get("bytes", 0) * 2
    detail["total_tiles"] = sum(r.get("tiles", 0) for r in detail["levels"])
    detail["total_bytes"] = sum(r.get("bytes", 0) for r in detail["levels"])


async def _probe_dem_max_level(bbox, provider: str) -> int | None:
    """探测 DEM 数据源在该范围的最高可用级别;None 表示探测失败(网络问题)。"""
    from ..providers.terrain import probe_max_level
    # 探测是阻塞网络请求,放线程池避免阻塞事件循环
    return await asyncio.to_thread(probe_max_level, bbox, provider)


async def _clamp_dem_levels(bbox, levels: list[int], provider: str
                            ) -> tuple[list[int], str]:
    """把超出 Esri 可用 LOD 的 DEM 级别下调到该范围真实可用的最高级。

    为什么必须做:Esri Terrain3D 各区域最高 LOD 不同(新疆一带实测只到 14 级),
    超限时服务返回 HTTP 200 + 67 字节"空瓦片"而非 404。下载器只看状态码与响应体
    非空,会把它当成功写进缓存;拼接阶段解码后全是 nodata,最终产出一张有效像素
    为 0 的高程 GeoTIFF 与一套平地地形切片——整个过程无任何报错,用户白等一场。

    返回 (最终级别列表, 提示信息)。提示为空串表示未做调整。
    """
    if not is_dem_provider(provider) or not levels:
        return levels, ""
    max_level = await _probe_dem_max_level(bbox, provider)
    if max_level is None or max(levels) <= max_level:
        return levels, ""
    kept = [z for z in levels if z <= max_level]
    dropped = [z for z in levels if z > max_level]
    # 全部超限时至少保留可用最高级,否则任务会因"没有级别"直接失败
    out = kept or [max_level]
    note = (f"所选范围在该地形数据源最高只有 {max_level} 级"
            f"(已跳过无数据的 {'、'.join(str(z) for z in dropped)} 级)")
    return out, note


router = APIRouter(prefix="/api/tasks", tags=["tasks"])


def _parse_levels(levels: str | None, z_min: int | None, z_max: int | None,
                  z_cap: int = 18, z_floor: int = 1) -> list[int]:
    """把查询串 levels(逗号分隔)解析成升序去重级别列表,回退 z_min..z_max。

    z_cap:级别上限(天地图 18,Esri DEM 16);z_floor:下限(天地图 1,DEM 0)。
    """
    if levels:
        out = {int(x) for x in levels.split(",") if x.strip().lstrip("-").isdigit()}
        return sorted(z for z in out if z_floor <= z <= z_cap)
    if z_min is not None and z_max is not None and z_min <= z_max:
        return list(range(max(z_min, z_floor), min(z_max, z_cap) + 1))
    return []


@router.get("")
async def api_list_tasks():
    return list_tasks()


@router.get("/estimate")
async def api_estimate(west: float, south: float, east: float, north: float,
                       levels: str | None = None, provider: str = "tianditu_img",
                       z_min: int | None = None, z_max: int | None = None,
                       annotate: bool = False, export: str | None = None):
    """提交前预估:返回每层瓦片数与估算大小及合计。

    levels 为逗号分隔级别,兼容旧的 z_min/z_max。total 字段保留向后兼容。

    annotate=True 时把注记瓦片算进来(只对 ≤ ANNOTATION_MAX_Z 的级别),
    与 api_create_task 的算法**必须一致** —— 此前本接口不接受 annotate,
    增量由前端整体 ×2 补,导致对话框预估与建成后的任务数对不上
    (z19+ 的注记增量为 0,整体 ×2 会虚高一倍)。

    export 为逗号分隔格式,决定**要下载哪些网格**。天地图同时勾 tms+osm 时会下
    两套原生瓦片(见 core.formats.download_grids_of),预估要按两套算 ——
    否则用户看到实际下载量是预估的两倍会以为出 bug。返回里的 grids 字段供前端
    显示"含双网格"。不传时与改动前一致(只算源自己的默认网格)。
    """
    z_cap = _z_cap_for(provider)
    lv = _parse_levels(levels, z_min, z_max, z_cap,
                       z_floor=_z_floor_for(provider))
    detail = _estimate_detail((west, south, east, north), lv, provider, export)
    if annotate and not is_dem_provider(provider):
        _add_annotation_to_detail(detail, lv)
    # total 保留:旧前端只读 total。
    # grids 是给界面用的(显示"含双网格,瓦片数已按两套计"),放在接口这一层而不是
    # _estimate_detail 里 —— 后者有测试做严格相等,不能凭添键。
    return {"total": detail["total_tiles"],
            "grids": _grids_for_estimate(provider, export), **detail}


@router.get("/suggest_levels")
async def api_suggest_levels(west: float, south: float, east: float, north: float,
                            provider: str = "tianditu_img"):
    """按选区大小建议下载级别,并给出各级的"有效数据占比"。

    用途:瓦片是固定网格,低级别单张就能盖住远超选区的范围(实测 0.07° 的选区在
    天地图第 7 级只有 0.1% 有效占比)。全选 1-18 会下一堆几乎全是选区外内容的图。
    """
    from ..core.tiling import suggest_levels

    bbox = (west, south, east, north)
    if is_dem_provider(provider):
        # DEM 是墨卡托 XYZ 网格,且预算/级别下限与影像不同
        from ..core.dem_tiling import suggest_dem_levels
        return await asyncio.to_thread(
            suggest_dem_levels, bbox, DEM_LAYERS[provider][2])
    if grid_of(provider) == GEO_MERCATOR:
        # 墨卡托影像:级别从 1 起、预算 8000(见 suggest_mercator_levels 的说明)
        return await asyncio.to_thread(
            suggest_mercator_levels, bbox, _z_cap_for(provider))
    return await asyncio.to_thread(suggest_levels, bbox, 18, 1)


@router.get("/dem_max_level")
async def api_dem_max_level(west: float, south: float, east: float, north: float,
                            provider: str = "esri_terrain"):
    """探测 DEM 数据源在该范围的最高可用级别(有真实数据的最大 LOD)。

    Esri Terrain3D 各区域最高级别不同(超出返回空瓦片),前端据此禁用超限级别。
    """
    if not is_dem_provider(provider):
        raise HTTPException(400, "该数据源不支持地形级别探测")
    service_max = DEM_LAYERS[provider][2]
    max_level = await _probe_dem_max_level((west, south, east, north), provider)
    # max_level 为 null 表示探测失败(网络问题),前端此时不应禁用任何级别
    return {"provider": provider, "max_level": max_level,
            "service_max": service_max}


@router.get("/imagery_max_level")
async def api_imagery_max_level(west: float, south: float, east: float,
                                north: float, provider: str = "esri_imagery"):
    """探测影像数据源在该范围的最高可用级别(前端据此禁用超限级别)。

    实测 Esri World Imagery 各区域最高级别不同:城市(含拉萨/乌鲁木齐)可到 z19、
    喀什/漠河 z18、西藏青海新疆无人区仅 z17(z18 即整片占位图)。不探测的话
    用户选 z18 在西部会下到一整片灰色。

    Google 不需要探测(陆地处处可到 z21),对它直接返回服务上限,省一次往返。
    max_level 为 null 表示探测失败(网络/代理问题)或探测已关闭,前端此时
    **不应禁用**任何级别 —— 与 dem_max_level 的既有约定一致。
    """
    if not (is_esri_imagery_provider(provider) or is_google_provider(provider)):
        raise HTTPException(400, "该数据源不支持影像级别探测")
    service_max = _z_cap_for(provider)
    if is_google_provider(provider):
        return {"provider": provider, "max_level": service_max,
                "service_max": service_max, "probed": False}
    max_level = await _probe_imagery_max_level(
        (west, south, east, north), provider)
    return {"provider": provider, "max_level": max_level,
            "service_max": service_max, "probed": True}


@router.get("/{task_id}")
async def api_get_task(task_id: str):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


def _dir_size(p: Path) -> int:
    """递归统计目录(或文件)占用字节。目录不存在返回 0。"""
    if not p.exists():
        return 0
    if p.is_file():
        try:
            return p.stat().st_size
        except OSError:
            return 0
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except OSError:
            pass
    return total


def _scan_output_size(task: dict) -> dict:
    """扫描任务导出目录,按成果类型分别统计磁盘占用(字节)并给出合计。

    分类对应用户实际关心的几类成果:
      geotiff  影像每级 GeoTIFF({name}_z*.tif,含另存的投影版 _{epsg}.tif)
      dem      高程 GeoTIFF({name}_dem_z*.tif) + 晕渲图({name}_hillshade_z*.tif)
      tms      TMS 瓦片包(tms/)
      osm      OSM 瓦片包(osm/)
      terrain  Cesium 地形切片(terrain/)
      tiles    保留的原始 LERC 瓦片(tiles/)
      b3dm     3D Tiles 瓦片集(3dtiles/;三维数据任务时是 OSGB 倾斜模型或点云 pnts)
      pc_dsm   点云 DSM GeoTIFF({name}_dsm.tif;点云 DEM 与建筑地面高程同归 bld_dem)
    返回 {items:[{key,label,bytes}], total_bytes, output_path}。
    下载的原始缓存瓦片是跨任务共享的中间产物,不计入单任务成果大小。
    """
    out = task.get("output_path")
    items: list[dict] = []
    if not out:
        return {"items": items, "total_bytes": 0, "output_path": ""}
    out_dir = Path(out)
    if not out_dir.is_dir():
        return {"items": items, "total_bytes": 0, "output_path": out}

    name = task.get("name") or ""
    provider = task.get("provider") or ""
    # 三维数据任务(OSGB 倾斜模型/点云)的同类成果换用中性标签,避免标成"建筑"
    is_model3d = provider in ("local_osgb", "local_pointcloud")

    # 影像 / 高程每级 GeoTIFF:按文件名前缀归类(排除 dem/hillshade 前缀避免重复计入)
    geotiff_bytes = 0
    dem_bytes = 0
    for f in out_dir.glob(f"{name}_*.tif"):
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        fname = f.name
        if fname == f"{name}_dem.tif":
            # 三维建筑任务的地面高程 / 点云任务的 DEM,下面单独归类,不计入影像 GeoTIFF
            continue
        if fname == f"{name}_dsm.tif":
            # 点云任务的 DSM,下面单独归类,不计入影像 GeoTIFF
            continue
        if fname.startswith(f"{name}_dem_z") or fname.startswith(f"{name}_hillshade_z"):
            dem_bytes += sz
        else:
            geotiff_bytes += sz

    def add(key: str, label: str, b: int):
        if b > 0:
            items.append({"key": key, "label": label, "bytes": b})

    add("geotiff", "影像 GeoTIFF", geotiff_bytes)
    add("dem", "高程/晕渲 GeoTIFF", dem_bytes)
    add("tms", "TMS 瓦片", _dir_size(out_dir / "tms"))
    add("osm", "OSM 瓦片", _dir_size(out_dir / "osm"))
    add("terrain", "Cesium 地形切片", _dir_size(out_dir / "terrain"))
    add("tiles", "原始 LERC 瓦片", _dir_size(out_dir / "tiles"))
    add("b3dm", "3D Tiles 瓦片集" if is_model3d else "三维建筑 3D Tiles",
        _dir_size(out_dir / "3dtiles"))
    # 三维建筑的另两类成果:建筑轮廓矢量与地面高程(点云任务的同名文件是仅地面点的 DEM)
    _vec = out_dir / f"{name}_buildings.geojson"
    add("bld_vector", "建筑轮廓矢量", _vec.stat().st_size if _vec.exists() else 0)
    _bdem = out_dir / f"{name}_dem.tif"
    add("bld_dem", "DEM GeoTIFF" if provider == "local_pointcloud" else "地面高程 GeoTIFF",
        _bdem.stat().st_size if _bdem.exists() else 0)
    # 点云任务的 DSM(数字表面模型,含地表附着物)
    _dsm = out_dir / f"{name}_dsm.tif"
    add("pc_dsm", "DSM GeoTIFF", _dsm.stat().st_size if _dsm.exists() else 0)

    total = sum(it["bytes"] for it in items)
    return {"items": items, "total_bytes": total, "output_path": out}


def _output_dir_for_mutation(task: dict) -> Path:
    """取任务输出目录,并限制在 output 根目录内。

    这类接口会原地修改成果文件,不能像只读扫描那样信任任意路径。
    """
    out = task.get("output_path") or ""
    if not out:
        raise HTTPException(400, "任务没有输出目录")
    out_dir = Path(out)
    if not out_dir.is_dir():
        raise HTTPException(404, f"任务输出目录不存在:{out_dir}")
    try:
        out_dir.resolve().relative_to(settings.output_dir.resolve())
    except (ValueError, OSError):
        raise HTTPException(403, "只能修改 output 目录内的任务成果")
    return out_dir


def _repair_task_output(task: dict) -> dict:
    """批量修复任务输出目录中的旧版 nodata=0 RGB GeoTIFF。"""
    from ..core.repair_nodata import repair_dir

    out_dir = _output_dir_for_mutation(task)
    fixed, recovered = repair_dir(out_dir)
    return {
        "output_path": str(out_dir),
        "fixed": fixed,
        "recovered_pixels": recovered,
    }


@router.get("/{task_id}/size")
async def api_task_size(task_id: str):
    """返回任务导出成果的磁盘占用明细(按格式分类)+ 合计。

    扫描磁盘为阻塞 IO,放线程池执行,避免阻塞事件循环。
    """
    import asyncio
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return await asyncio.to_thread(_scan_output_size, task)


@router.post("/{task_id}/repair_nodata")
async def api_repair_nodata(task_id: str):
    """修复旧版裁剪影像的 nodata=0 白点问题。

    早期成果把边界外标成 nodata=0,QGIS 会把 RGB 中任一波段为 0 的合法像素也
    当无数据渲成白点。修复只清元数据并写 GDAL 掩膜,不重写像素值。
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("done", "failed"):
        raise HTTPException(
            400, f"只有已完成或部分失败的任务成果才能修复(当前 {task['status']})")
    return await asyncio.to_thread(_repair_task_output, task)


@router.get("/{task_id}/layers")
async def api_task_layers(task_id: str):
    """列出该任务可叠加到地图的图层(或可打开三维预览的成果)。

    扫盘 + 读栅格元信息是阻塞 IO,放线程池。
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    from ..core.overlay import list_layers
    layers = await asyncio.to_thread(list_layers, task, settings.output_dir)
    return {"id": task_id, "name": task["name"], "layers": layers}


@router.get("/{task_id}/vector/{filename}")
async def api_task_vector(task_id: str, filename: str):
    """把成果里的 gpkg/shp 转成 GeoJSON 返回,供地图叠加。

    浏览器读不了这两种格式,只能由后端转。不落盘、直接返回内容——这是"看一下"
    的用途,持久化成果应该走导出功能。
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    out_dir = Path(task.get("output_path") or "")
    p = (out_dir / filename)
    # 防路径穿越:filename 来自 URL,必须确认解析结果仍在成果目录内
    try:
        p.resolve().relative_to(out_dir.resolve())
    except (ValueError, OSError):
        raise HTTPException(400, "非法文件名")
    if not p.is_file():
        raise HTTPException(404, f"文件不存在:{filename}")
    if p.suffix.lower() not in (".gpkg", ".shp", ".fgb"):
        raise HTTPException(400, f"不支持转换该格式:{p.suffix}")

    def _to_geojson() -> dict:
        import json as _json
        from pyogrio.raw import read as _read
        from shapely import wkb as _wkb
        from shapely.geometry import mapping as _mapping
        meta, _idx, geoms, field_data = _read(str(p))
        names = [str(x) for x in meta["fields"]]
        feats = []
        for i, g in enumerate(geoms):
            if g is None:
                continue
            try:
                geom = _mapping(_wkb.loads(g))
            except Exception:
                continue
            props = {}
            for j, nm in enumerate(names):
                v = field_data[j][i]
                # numpy 标量不是 JSON 可序列化类型
                props[nm] = v.item() if hasattr(v, "item") else v
            feats.append({"type": "Feature", "geometry": geom, "properties": props})
        return {"type": "FeatureCollection", "features": feats}

    try:
        return await asyncio.to_thread(_to_geojson)
    except Exception as e:
        raise HTTPException(500, f"转换失败:{str(e)[:200]}") from e


# ---------- MBTiles 预览 ----------
# 成果目录本身由 /output 静态挂载,但 MBTiles 是单个 sqlite 文件,浏览器不能
# 直接按 {z}/{x}/{y} 取瓦片。故这里提供瓦片端点,让预览页像读瓦片目录一样读它。

def _mbtiles_path(task: dict, kind: str) -> Path:
    """定位任务的 MBTiles 文件。kind 为 tms / osm。"""
    if kind not in ("tms", "osm"):
        raise HTTPException(400, f"未知的瓦片类型:{kind}")
    out_dir = Path(task.get("output_path") or "")
    if not out_dir.is_dir():
        raise HTTPException(404, "任务输出目录不存在")
    p = out_dir / f"{task['name']}_{kind}.mbtiles"
    if not p.is_file():
        raise HTTPException(404, f"未找到 {kind} 的 MBTiles 成果")
    # 名称来自任务名(已经 safe_dirname 清理过),仍做一次归属校验兜底,
    # 确保解析结果没跑出任务目录之外。
    try:
        p.resolve().relative_to(out_dir.resolve())
    except ValueError:
        raise HTTPException(400, "非法路径")
    return p


@router.get("/{task_id}/tiles_form/{kind}")
async def api_tiles_form(task_id: str, kind: str):
    """告知某瓦片阶段的成果以哪种形态存在:散列目录 / MBTiles / 两者都有。

    预览页据此决定怎么取瓦片。为什么不让前端自己探目录:/output 是 StaticFiles
    挂载,对"存在的目录"和"不存在的目录"都返回 404(未开目录列表),前端无法区分。
    """
    if kind not in ("tms", "osm"):
        raise HTTPException(400, f"未知的瓦片类型:{kind}")
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    out_dir = Path(task.get("output_path") or "")
    has_dir = (out_dir / kind).is_dir()
    has_mb = (out_dir / f"{task['name']}_{kind}.mbtiles").is_file()
    return {"dir": has_dir, "mbtiles": has_mb}


@router.get("/{task_id}/mbtiles/{kind}/meta")
async def api_mbtiles_meta(task_id: str, kind: str):
    """返回 MBTiles 的级别范围与瓦片格式,供预览页构造图层。"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    from ..core.mbtiles import read_metadata
    meta = await asyncio.to_thread(read_metadata, _mbtiles_path(task, kind))
    if not meta:
        raise HTTPException(404, "MBTiles 无法读取")
    return {
        "minzoom": int(meta.get("minzoom") or 0),
        "maxzoom": int(meta.get("maxzoom") or 0),
        "format": meta.get("format") or "png",
        # TMS 包是 EPSG:4326 geodetic 网格,OSM 包是 Web 墨卡托,前端要用不同 tilingScheme
        "profile": meta.get("profile") or ("geodetic" if kind == "tms" else "mercator"),
        "bounds": meta.get("bounds") or "",
    }


@router.get("/{task_id}/mbtiles/{kind}/{z}/{x}/{y}")
async def api_mbtiles_tile(task_id: str, kind: str, z: int, x: int, y: int):
    """从 MBTiles 取单张瓦片。y 按请求方约定:tms 包自南向北,osm 包自北向南。"""
    from fastapi import Response

    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    from ..core.mbtiles import read_tile
    path = _mbtiles_path(task, kind)
    data = await asyncio.to_thread(read_tile, path, z, x, y)
    if data is None:
        # 瓦片不存在是正常情况(范围外),返回 204 让前端当空白处理,不刷错误日志
        return Response(status_code=204)
    # 按实际内容判断类型:jpg 以 FF D8 开头,png 以 89 50 开头
    media = "image/jpeg" if data[:2] == b"\xff\xd8" else "image/png"
    return Response(content=data, media_type=media,
                    headers={"Cache-Control": "max-age=3600"})


# 三维建筑范围上限(平方度)。建筑要素全程在内存/中间文件里流转,范围过大会
# 拖垮内存与耗时;Overpass 还要按 0.05° 分块逐块请求,块数随面积平方增长。
# 1 平方度在中低纬约 1.2 万 km²,4 平方度足够覆盖一个特大城市群
# (对应 Overpass 约 1600 块请求,已是很长的任务)。
_BUILDINGS_MAX_SPAN_DEG2 = 4.0


async def _create_buildings_task(data: TaskCreate):
    """三维建筑白模建任务。

    与栅格任务的差别:没有级别与瓦片计数,进度分母是建筑栋数(取数阶段动态确定);
    这里只做范围与参数合法性校验,不预先查远端数据源(跨境查询可能很慢)。
    """
    from ..core.buildings import BaseHeightMode
    from ..core.vector_upload import analyze, load_upload, upload_exists
    from ..providers.local_vector import HeightMode

    if data.base_height_mode not in BaseHeightMode.ALL:
        raise HTTPException(400, f"未知底面高模式:{data.base_height_mode}")

    # 上传地形:仅 terrain 模式有意义,且必须确实存在
    if data.dem_upload_id:
        from ..core import dem_upload
        if data.base_height_mode != BaseHeightMode.TERRAIN:
            raise HTTPException(
                400, "只有底面高模式为「采样地形」时才能使用上传的地形数据")
        if not dem_upload.exists(data.dem_upload_id):
            raise HTTPException(400, "上传的地形数据不存在或已过期,请重新上传")

    is_local = data.provider == "local_vector"
    if is_local:
        if not data.upload_id:
            raise HTTPException(400, "请先上传矢量面数据")
        if not upload_exists(data.upload_id):
            raise HTTPException(400, "上传的矢量数据不存在或已过期,请重新上传")
        if data.height_mode not in HeightMode.ALL:
            raise HTTPException(400, f"未知高度模式:{data.height_mode}")
        if data.height_mode != HeightMode.NONE and not data.height_field:
            raise HTTPException(400, "已选择按字段取高度,请指定高度字段")

    west, south, east, north = data.bbox
    if east <= west or north <= south:
        # 本地矢量未画范围时,用上传数据自身的 bbox(数据现成,不必先框范围)
        if is_local:
            info = await asyncio.to_thread(
                lambda: analyze(load_upload(data.upload_id)))
            if not info.get("bbox"):
                raise HTTPException(400, "上传数据中没有有效的面要素范围")
            west, south, east, north = info["bbox"]
            data.bbox = [west, south, east, north]
        else:
            raise HTTPException(400, "范围无效,请重新选择")

    # 本地矢量的数据量由上传文件界定(要素数已在上传时限制),面积上限对它无意义:
    # 一份全省房屋轮廓轻易超过 4 平方度,但要素数可能并不大。
    if not is_local:
        span = (east - west) * (north - south)
        if span > _BUILDINGS_MAX_SPAN_DEG2:
            raise HTTPException(
                400,
                f"三维建筑范围过大(约 {span:.1f} 平方度,上限 {_BUILDINGS_MAX_SPAN_DEG2:.0f}),"
                "请缩小范围后再试")

    # total 未知(取数阶段才知道建筑栋数),先置 0;est_bytes 同理不预估
    task_id = create_task(data, 0, 0)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": 0, "status": "pending"}


@router.post("")
async def api_create_task(data: TaskCreate):
    # 三维建筑:数据是矢量要素集,无瓦片级别与瓦片计数,也不需要天地图密钥
    if is_building_provider(data.provider):
        return await _create_buildings_task(data)

    # 本地文件源:不下载不联网,校验与瓦片计数都另走一套
    from ..core.formats import is_local_source
    if is_local_source(data.provider):
        return await _create_local_task(data)

    dem = is_dem_provider(data.provider)
    # 需要天地图密钥的只有天地图数据源:DEM 走 Esri 公开服务、Google/Esri 影像
    # 用非官方端点(且忽略 key 参数),都不校验密钥。
    needs_tdt_token = not (dem or is_google_provider(data.provider)
                           or is_esri_imagery_provider(data.provider))
    if (needs_tdt_token and not settings.tianditu.token
            and not token_pool.has_any()):
        raise HTTPException(400, "未配置天地图密钥,请在密钥管理中添加,或在 config.yaml 填写 tianditu.token")

    # Google/Esri 需要代理才能下载。不预检的话失败会推迟到 worker 里,
    # 表现为"任务跑起来又全部瓦片失败",且白占一个 worker 槽位数分钟。
    ok, msg = await _precheck_network_provider(data.provider)
    if not ok:
        raise HTTPException(400, msg)

    levels = data.level_list()
    if not levels:
        raise HTTPException(400, "请至少选择一个下载级别")

    west, south, east, north = data.bbox
    # DEM 超出该范围真实可用 LOD 的级别会拼出全 nodata 成果,提交时就下调
    levels, level_note = await _clamp_dem_levels(
        (west, south, east, north), levels, data.provider)
    if level_note:
        data.levels = levels
        logger.info("任务[%s] %s", data.name, level_note)

    # Esri 各区域最高级别不同(西部无人区仅 z17)。超限级别会整片下到占位图,
    # 故提交时剔除 —— 剔除而非拒绝,并把结果回报给用户。
    dropped_levels: list[int] = []
    if is_esri_imagery_provider(data.provider):
        probed = await _probe_imagery_max_level(
            (west, south, east, north), data.provider)
        kept, dropped_levels = _trim_levels_for_region(levels, probed)
        if dropped_levels:
            levels = kept
            data.levels = kept
            logger.info("任务[%s] 剔除超出该区域能力的级别 %s(区域最高 %s)",
                        data.name, dropped_levels, probed)
    detail = _estimate_detail((west, south, east, north), levels, data.provider,
                              data.export)
    total = detail["total_tiles"]
    if total == 0:
        raise HTTPException(400, "所选范围在该级别下没有瓦片,请检查范围或级别")
    # 预估原始瓦片下载量(字节):仅下载量,非成果大小
    est_bytes = detail["total_bytes"]
    # 叠加注记要额外下载同网格的注记瓦片,进度分母与体积都要算上。
    # ⚠️ 不能整体翻倍:天地图注记最高 z18(见 ANNOTATION_MAX_Z),
    # Google 选 z19~21 时注记一张都不下,翻倍会虚高一倍。
    # 用与 /api/tasks/estimate 相同的函数,保证"对话框预估 = 建成的任务数"。
    if data.annotate and not dem:
        _add_annotation_to_detail(detail, levels)
        total = detail["total_tiles"]
        est_bytes = detail["total_bytes"]

    task_id = create_task(data, total, est_bytes)
    await task_queue.enqueue(task_id)
    resp = {"id": task_id, "total": total, "status": "pending",
            "levels": levels, "level_note": level_note}
    if dropped_levels:
        # 回报给用户,避免"我勾了 z18 却没出成果"的困惑(静默丢功能)
        resp["dropped_levels"] = dropped_levels
        resp["level_note"] = (
            (level_note + ";" if level_note else "")
            + "该区域 Esri 影像最高仅到 z%s,已跳过 %s"
            % (max(levels),
               ", ".join("z%d" % z for z in dropped_levels)))
    return resp


async def _create_local_task(data: TaskCreate):
    """本地文件源建任务:不下载,直接处理用户磁盘上的文件。

    与下载任务的差别:
      - 不校验天地图密钥(不联网)
      - total 不是"要下载的瓦片数"而是 0(没有下载阶段,进度分母由各导出阶段自报)
      - bbox 缺省时取源文件自身范围(用户可能不画范围就想整幅处理)
      - 级别缺省时取源文件的原生级别(本地文件只有一个分辨率)
    """
    from ..core import local_raster

    p = Path((data.source_path or "").strip().strip('"'))
    if not p.is_absolute():
        raise HTTPException(400, f"源路径不是绝对路径:{p}")

    # 三维源(OSGB 目录 / 点云 las-laz)不是栅格:跳过栅格 inspect 与范围交集,另走一套
    # provider 名单不硬编码,从 formats 注册表按数据类型推导(单一事实源)
    from ..core.formats import DataKind, kind_of
    if kind_of(data.provider) in (DataKind.MESH_OSGB, DataKind.POINT_CLOUD):
        return await _create_local_3d_task(data, p)

    if not p.is_file():
        raise HTTPException(400, f"源文件不存在:{p}")

    try:
        info = await asyncio.to_thread(local_raster.inspect_for_import, p)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(400, f"无法读取源栅格:{str(e)[:200]}") from e

    # 数据类型必须与 provider 对得上,否则阶段与成果会错位
    want = ("local_dem" if info["kind"] == "raster_dem" else "local_image")
    if data.provider != want:
        raise HTTPException(
            400, f"该文件被判定为{'高程' if want == 'local_dem' else '影像'}数据"
                 f"({info['kind_reason']}),请选择对应的数据类型")

    # bbox 缺省用源文件范围;给了则取与源范围的交集(超出部分没有数据)
    src_bbox = info["bounds_wgs84"]
    if data.bbox and len(data.bbox) == 4 and any(data.bbox):
        w = max(data.bbox[0], src_bbox[0]); s = max(data.bbox[1], src_bbox[1])
        e = min(data.bbox[2], src_bbox[2]); n = min(data.bbox[3], src_bbox[3])
        if e <= w or n <= s:
            raise HTTPException(400, "所选范围与该文件的数据范围没有交集")
        data.bbox = [w, s, e, n]
    else:
        data.bbox = list(src_bbox)

    if not data.level_list():
        data.levels = [int(info["native_level"])]

    task_id = create_task(data, 0, 0)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": 0, "status": "pending",
            "kind": info["kind"], "levels": data.level_list()}


def _dir_has_las(p: Path) -> bool:
    """递归枚举目录,是否含至少一个 las/laz 文件。

    同步遍历,调用方需用 asyncio.to_thread 放到线程里跑:
    大目录且无 las 时全量 rglob 会阻塞事件循环(单进程 uvicorn 整体冻结)。
    """
    return any(f.suffix.lower() in (".las", ".laz")
               for f in p.rglob("*") if f.is_file())


#: pc_crs 合法形式:空串(自动读 LAS 头)、local(本地坐标)、EPSG:数字;
#: 大小写均不敏感。其余形式(裸数字/WKT/带空格等) runner 阶段无法处理,
#: 在建任务时就拦下,避免入队后才失败。
_PC_CRS_RE = re.compile(r"^(?:EPSG:\d+|local)$", re.IGNORECASE)


async def _create_local_3d_task(data: TaskCreate, p: Path):
    """本地三维源(OSGB / 点云)建任务:跳过栅格 inspect,只做存在性与形态校验。

    - local_osgb:OSGB 是目录结构(Data/ + metadata.xml),只接受已存在的目录
    - local_pointcloud:接受单个 las/laz 文件,或包含至少一个 las/laz 的目录(递归枚举)
    bbox/级别此时读不出来(OSGB metadata、LAS 头要到 runner 阶段才解析),
    bbox 缺省置 [0,0,0,0] 占位;没有下载阶段,total 恒为 0。
    """
    pc_crs = data.pc_crs or ""
    if pc_crs and not _PC_CRS_RE.match(pc_crs):
        raise HTTPException(
            400, f"pc_crs 只支持 EPSG:数字 或 local(收到:{pc_crs})")
    if data.provider == "local_osgb":
        if not p.is_dir():
            raise HTTPException(400, f"OSGB 数据源需要选择已存在的目录:{p}")
    else:  # local_pointcloud
        if p.is_dir():
            # 目录枚举放线程里跑,避免阻塞事件循环(同栅格分支 inspect_for_import 的写法)
            if not await asyncio.to_thread(_dir_has_las, p):
                raise HTTPException(400, f"目录下没有找到 las/laz 点云文件:{p}")
        elif not p.is_file():
            raise HTTPException(400, f"点云文件不存在:{p}")
        elif p.suffix.lower() not in (".las", ".laz"):
            raise HTTPException(400, f"点云数据源只支持 las/laz 文件:{p}")

    if not data.bbox or len(data.bbox) != 4:
        data.bbox = [0.0, 0.0, 0.0, 0.0]

    task_id = create_task(data, 0, 0)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": 0, "status": "pending"}


async def _update_buildings_task(task_id: str, data: TaskCreate):
    """三维建筑任务就地重跑:改建模参数并重新入队,沿用原范围与输出目录。

    阶段重建时按"参数是否影响该阶段"决定 reset:
      - 底面高模式/偏移变了 → base_dem 与 build_mesh 之后全部重做
      - 只改单瓦片建筑数 → 仅 tile_3d 重做(取数与建模成果可复用)
    """
    from ..core.buildings import BaseHeightMode

    if data.base_height_mode not in BaseHeightMode.ALL:
        raise HTTPException(400, f"未知底面高模式:{data.base_height_mode}")

    task = get_task(task_id)
    old_mode = task.get("base_height_mode") or BaseHeightMode.TERRAIN
    old_offset = float(task.get("height_offset") or 0.0)
    old_default = float(task.get("default_height") or 6.0)
    base_changed = (
        old_mode != data.base_height_mode
        or abs(old_offset - float(data.height_offset)) > 1e-9
        or abs(old_default - float(data.default_height)) > 1e-9
        # 换了上传地形 → 底面高全变,base_dem 与建模都要重做
        or (data.dem_upload_id or "") != (task.get("dem_upload_id") or "")
    )
    # 字段映射变更需重跑取数:属性(保留字段)与高度都是在取数阶段附加到要素上的,
    # 只重跑建模会沿用旧的 _buildings_raw.jsonl,改动不会生效。
    mapping_changed = (
        data.provider == "local_vector" and (
            (data.upload_id or "") != (task.get("upload_id") or "")
            or (data.height_field or "") != (task.get("height_field") or "")
            or (data.height_mode or "none") != (task.get("height_mode") or "none")
            or abs(float(data.height_scale) - float(task.get("height_scale") or 1.0)) > 1e-9
            or abs(float(data.floor_height) - float(task.get("floor_height") or 3.0)) > 1e-9
            or (data.name_field or "") != (task.get("name_field") or "")
            or list(data.keep_fields or []) != list(task.get("keep_fields") or [])
        )
    )

    stages = build_stage_defs(data.provider, parse_export(data.export),
                              data.annotate, data.base_height_mode)
    for s in stages:
        if s["key"] == "fetch_buildings" and not mapping_changed:
            continue          # 轮廓数据与参数无关,保留中间文件即可复用
        if mapping_changed or base_changed or s["key"] == "tile_3d":
            s["reset"] = True

    update_task(
        task_id,
        provider=data.provider,
        export=data.export,
        base_height_mode=data.base_height_mode,
        height_offset=float(data.height_offset),
        default_height=float(data.default_height),
        max_per_tile=int(data.max_per_tile),
        # 本地矢量的字段映射也可改(换高度字段/增减保留字段后重切)
        upload_id=data.upload_id or task.get("upload_id") or "",
        height_field=data.height_field or "",
        height_mode=data.height_mode or "none",
        height_scale=float(data.height_scale),
        floor_height=float(data.floor_height),
        name_field=data.name_field or "",
        keep_fields=json.dumps(data.keep_fields or [], ensure_ascii=False),
        dem_upload_id=data.dem_upload_id or "",
        stages=json.dumps(stages),
        total=0, downloaded=0, failed=0, building_count=0,
        status="pending", message="已重新加入队列",
    )
    task_queue.clear_control(task_id)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": 0, "status": "pending"}


@router.put("/{task_id}")
async def api_update_task(task_id: str, data: TaskCreate):
    """就地重新下载:修改 paused/failed/canceled 任务的参数并重新入队。

    沿用原任务的输出目录与范围(bbox/geometry),不新建任务。
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("paused", "failed", "canceled"):
        raise HTTPException(400, f"当前状态({task['status']})不支持就地重新下载")

    # 三维建筑:无级别/瓦片计数,改的是底面高模式与建模参数
    if is_building_provider(data.provider) or is_building_provider(task["provider"]):
        return await _update_buildings_task(task_id, data)

    levels = data.level_list()
    if not levels:
        raise HTTPException(400, "请至少选择一个下载级别")

    # 范围沿用原任务(前端不改范围);用原 bbox 重新估算总数
    west, south, east, north = task["bbox"]
    # 与新建任务同理:DEM 超限级别只会拼出全 nodata,重下时同样下调
    levels, level_note = await _clamp_dem_levels(
        (west, south, east, north), levels, data.provider)
    if level_note:
        logger.info("任务[%s] %s", task["name"], level_note)
    total = _estimate_total((west, south, east, north), levels, data.provider,
                            data.export)
    if total == 0:
        raise HTTPException(400, "所选范围在该级别下没有瓦片,请检查范围或级别")
    if data.annotate and not is_dem_provider(data.provider):
        total *= 2

    # 预估原始瓦片下载量(仅下载体积,非成果大小)
    est = _estimate_detail((west, south, east, north), levels, data.provider,
                           data.export)
    est_bytes = est.get("total_bytes", 0)
    if data.annotate and not is_dem_provider(data.provider):
        est_bytes *= 2

    # 参数已变(级别/格式可能不同),阶段全部重建为 pending。
    # 导出阶段标 reset:旧产出可能与新参数不符,runner 先清空该阶段目录再切。
    stages = build_stage_defs(data.provider, parse_export(data.export), data.annotate)
    for s in stages:
        if s["key"] != "download":
            s["reset"] = True
    update_task(
        task_id,
        provider=data.provider,
        z_min=levels[0], z_max=levels[-1],
        levels=json.dumps(levels),
        export=data.export,
        crs=data.crs or "EPSG:4326",
        clip=1 if data.clip else 0,
        use_cache=1 if data.use_cache else 0,
        annotate=1 if data.annotate else 0,
        tms_source_strategy=normalize_tms_source_strategy(
            data.tms_source_strategy),
        hillshade=json.dumps(data.hillshade.model_dump()),
        stages=json.dumps(stages),
        est_bytes=est_bytes,
        total=total, downloaded=0, failed=0,
        status="pending", message="已重新加入队列",
    )
    task_queue.clear_control(task_id)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": total, "status": "pending",
            "levels": levels, "level_note": level_note}


@router.post("/{task_id}/pause")
async def api_pause_task(task_id: str):
    """暂停任务。running 的协作式停止;pending 的直接标记暂停(出队时会跳过重排)。"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("running", "pending"):
        raise HTTPException(400, f"当前状态({task['status']})不可暂停")
    task_queue.request_pause(task_id)
    if task["status"] == "pending":
        # 尚未开始,直接落库为 paused(worker 出队时因 control=pause 不会执行)
        update_task(task_id, status="paused", message="已暂停")
    return {"id": task_id, "status": "paused"}


@router.post("/{task_id}/resume")
async def api_resume_task(task_id: str):
    """开始/继续任务。paused/failed/canceled → 重新入队,靠瓦片缓存断点续传。

    把所有未完成(非 done/skipped)的阶段重置为 pending,已完成阶段保留,
    runner 会跳过已完成阶段、只续跑未完成的。
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("paused", "failed", "canceled"):
        raise HTTPException(400, f"当前状态({task['status']})不可开始")
    stages = task.get("stages") or []
    for s in stages:
        if s.get("status") not in ("done", "skipped"):
            s["status"] = "pending"
            s["percent"] = 0.0
            s["eta_sec"] = None
    task_queue.clear_control(task_id)
    update_task(task_id, status="pending", message="已加入队列",
                stages=json.dumps(stages))
    await task_queue.enqueue(task_id)
    return {"id": task_id, "status": "pending"}


class AddExportReq(BaseModel):
    """给已完成任务补充导出格式。"""
    export: str = Field(..., description="要新增的格式,逗号分隔(如 osm,contour)")
    containers: dict[str, str] = Field(default_factory=dict)
    contour_interval: float = Field(default=0.0, gt=-1.0, le=10000.0)
    keep_tiles_dir: bool | None = Field(default=None)
    tms_source_strategy: str | None = Field(default=None)


@router.post("/{task_id}/add_export")
async def api_add_export(task_id: str, data: AddExportReq):
    """给已完成的任务补充新的导出格式,复用已有中间成果。

    为什么需要这个:此前想给已完成任务补一个格式,只能新建任务重下一遍——大范围
    影像可能是几十分钟。而下载的瓦片缓存与合并好的 GeoTIFF 都还在,新格式所需的
    上游数据其实已经具备,只是没有入口把新阶段接进已有任务。

    与 stage/retry 的区别:retry 只能重跑**已存在**的阶段;这里是往 stages 里
    **追加**新阶段。已完成的旧阶段保持 done,runner 会跳过它们、只跑新增的。
    """
    from ..core.formats import (ALL_FORMAT_NAMES, STAGES, is_local_source,
                                validate)

    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("done", "failed"):
        raise HTTPException(
            400, f"只有已完成或部分失败的任务才能补充导出(当前 {task['status']})")
    if is_building_provider(task["provider"]):
        raise HTTPException(
            400, "三维建筑任务的阶段是固定管线,不支持补充导出;"
                 "如需其他矢量格式请用「本地文件」功能转换已产出的建筑轮廓")

    provider = task["provider"]
    # 不能直接用 parse_export:它会把非法值过滤掉并回落成 geotiff,导致
    # "加 terrain 给影像任务"和"传空字符串"都变成"加 geotiff",报错文案就成了
    # 误导性的"已经导出过"。故先按原样切分,逐个校验。
    raw = [p.strip().lower() for p in
           (data.export or "").replace("+", ",").split(",") if p.strip()]
    if not raw:
        raise HTTPException(400, "未指定要新增的导出格式")

    want, unknown, unsupported = [], [], []
    for fmt in raw:
        key = stage_key_for_fmt(provider, fmt)
        if key is None:
            # 该 kind 的映射表里没有:要么格式名拼错,要么该数据源不支持
            (unknown if fmt not in ALL_FORMAT_NAMES else unsupported).append(fmt)
            continue
        want.append(fmt)
    if unknown:
        raise HTTPException(400, "未知的导出格式:" + "、".join(unknown))
    if unsupported:
        raise HTTPException(
            400, "该数据源不支持这些格式:" + "、".join(unsupported))

    # 容器合法性(阶段与容器的搭配)由注册表统一校验
    errs = validate(provider,
                    [stage_key_for_fmt(provider, f) for f in want],
                    data.containers or None)
    if errs:
        raise HTTPException(400, "；".join(errs))

    # 合并格式:旧的保留(它们的成果还在),新的追加
    old_fmts = parse_export(task.get("export", "geotiff"))
    merged = list(dict.fromkeys([*old_fmts, *want]))

    # 容器选择合并。注意不能改旧阶段已用的容器——那些成果已经按旧容器写出,
    # 改了只会让 metadata 与磁盘上的文件对不上。
    containers = dict(task.get("containers") or {})
    old_stage_keys = {s["key"] for s in (task.get("stages") or [])}
    for k, v in (data.containers or {}).items():
        if k in old_stage_keys:
            continue
        containers[k] = v

    interval = (data.contour_interval if data.contour_interval > 0
                else float(task.get("contour_interval") or 50.0))
    keep_dir = (task.get("keep_tiles_dir", True)
                if data.keep_tiles_dir is None else data.keep_tiles_dir)
    extra_updates = {}
    if data.tms_source_strategy is not None:
        extra_updates["tms_source_strategy"] = normalize_tms_source_strategy(
            data.tms_source_strategy)

    # 按合并后的格式重算阶段表:已有阶段沿用其状态(done 的会被 runner 跳过),
    # 新阶段为 pending。顺序由注册表的 order 决定,不受追加顺序影响。
    fresh = build_stage_defs(provider, merged, task.get("annotate", False),
                            task.get("base_height_mode") or "terrain")
    old_by_key = {s["key"]: s for s in (task.get("stages") or [])}
    stages = []
    added = []
    for s in fresh:
        prev = old_by_key.get(s["key"])
        if prev is not None:
            stages.append(prev)          # 保留原状态与进度
        else:
            stages.append(s)
            added.append(s["key"])
    if not added:
        raise HTTPException(400, "这些格式该任务已经导出过,无需补充")

    task_queue.clear_control(task_id)
    update_task(task_id, status="pending",
                message="补充导出:" + "、".join(
                    STAGES[k].label if k in STAGES else k for k in added),
                export=",".join(merged),
                containers=json.dumps(containers, ensure_ascii=False),
                contour_interval=interval,
                keep_tiles_dir=1 if keep_dir else 0,
                stages=json.dumps(stages), **extra_updates)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "status": "pending", "added": added,
            "export": ",".join(merged)}


def stage_key_for_fmt(provider: str, fmt: str) -> str | None:
    """格式名 → 阶段 key(按该 provider 的数据类型解析)。"""
    from ..core.formats import kind_of, stage_key_for_format
    return stage_key_for_format(kind_of(provider), fmt)


@router.post("/{task_id}/stage/{stage_key}/retry")
async def api_retry_stage(task_id: str, stage_key: str, purge: bool = False):
    """单阶段重跑:把指定阶段置 pending 后重新入队。

    各阶段相互独立,只重置该阶段;下载阶段保持 done(除非它本身失败)。
    重新入队后 runner 跳过其余 done 阶段,只跑被重置的阶段。

    purge=False(默认):续切——保留已切成果,跳过已存在瓦片,只补未完成的。
    purge=True:删除并重试——先清空该阶段旧产出再从头切(设 reset 标记)。
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    if task["status"] not in ("done", "failed", "paused", "canceled"):
        raise HTTPException(400, f"当前状态({task['status']})不支持阶段重试")
    stages = task.get("stages") or []
    found = False
    for s in stages:
        if s.get("key") == stage_key:
            s["status"] = "pending"
            s["percent"] = 0.0
            s["done"] = 0
            s["eta_sec"] = None
            s["message"] = "等待删除重试" if purge else "等待续切"
            # purge=True 才设 reset(清空旧产出重来);否则续切(保留已切瓦片)
            if purge:
                s["reset"] = True
            else:
                s.pop("reset", None)
            found = True
            break
    if not found:
        raise HTTPException(404, f"任务无此阶段:{stage_key}")
    # 若重试下载阶段,清空下载计数以便重新统计
    extra = {}
    if stage_key == "download":
        extra = {"downloaded": 0, "failed": 0}
    task_queue.clear_control(task_id)
    update_task(task_id, status="pending", message=f"重试阶段:{stage_key}",
                stages=json.dumps(stages), **extra)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "status": "pending", "stage": stage_key}


@router.delete("/{task_id}")
async def api_delete_task(task_id: str, purge: bool = False):
    """删除任务。running 的先请求取消;purge=True 时一并删除导出目录。"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    # 运行中先请求取消,让 runner 尽快停止(缓存瓦片保留)
    if task["status"] in ("running", "pending"):
        task_queue.request_cancel(task_id)
    # 可选:删除导出成果目录
    if purge and task.get("output_path"):
        out = Path(task["output_path"])
        try:
            if out.is_dir() and out.resolve().is_relative_to(settings.output_dir.resolve()):
                shutil.rmtree(out, ignore_errors=True)
        except Exception:
            pass
    delete_task(task_id)
    return {"id": task_id, "deleted": True}
