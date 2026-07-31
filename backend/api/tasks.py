"""任务相关 REST API。"""
from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException

from ..config import settings
from ..core.dem_tiling import estimate_dem_tiles, mercator_range_for_bbox
from ..core.queue import task_queue
from ..core.tiling import estimate_levels, estimate_levels_detail, estimate_total_tiles
from ..core.token_pool import token_pool
from ..models import (
    TaskCreate, build_stage_defs, create_task, delete_task, get_task,
    list_tasks, parse_export, update_task,
)
from ..providers.buildings import is_building_provider
from ..providers.terrain import DEM_LAYERS, is_dem_provider


def _estimate_total(bbox, levels: list[int], provider: str) -> int:
    """按数据源选择瓦片计数方式:DEM 用墨卡托 XYZ,天地图用 4326。"""
    if is_dem_provider(provider):
        return estimate_dem_tiles(bbox, levels)
    return estimate_levels(bbox, levels)


# DEM 单瓦片平均字节数(Esri Terrain3D LERC 经验值,约 40-90KB)
_DEM_AVG_BYTES = 60 * 1024


def _estimate_detail(bbox, levels: list[int], provider: str) -> dict:
    """预估明细。DEM 用墨卡托网格逐层计数;其余走天地图 4326 明细。"""
    if not is_dem_provider(provider):
        return estimate_levels_detail(bbox, levels, provider)
    w, s, e, n = bbox
    per = []
    total_tiles = 0
    for z in sorted(set(levels)):
        tiles = mercator_range_for_bbox(w, s, e, n, z).count
        per.append({"z": z, "tiles": tiles, "bytes": tiles * _DEM_AVG_BYTES})
        total_tiles += tiles
    return {"levels": per, "total_tiles": total_tiles,
            "total_bytes": total_tiles * _DEM_AVG_BYTES}

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
                       z_min: int | None = None, z_max: int | None = None):
    """提交前预估:返回每层瓦片数与估算大小及合计。

    levels 为逗号分隔级别,兼容旧的 z_min/z_max。total 字段保留向后兼容。
    """
    dem = is_dem_provider(provider)
    z_cap = DEM_LAYERS[provider][2] if dem else 18
    lv = _parse_levels(levels, z_min, z_max, z_cap, z_floor=0 if dem else 1)
    detail = _estimate_detail((west, south, east, north), lv, provider)
    # total 保留:旧前端只读 total
    return {"total": detail["total_tiles"], **detail}


@router.get("/suggest_levels")
async def api_suggest_levels(west: float, south: float, east: float, north: float,
                            provider: str = "tianditu_img"):
    """按选区大小建议下载级别,并给出各级的"有效数据占比"。

    用途:瓦片是固定网格,低级别单张就能盖住远超选区的范围(实测 0.07° 的选区在
    天地图第 7 级只有 0.1% 有效占比)。全选 1-18 会下一堆几乎全是选区外内容的图。
    """
    from ..core.tiling import suggest_levels

    dem = is_dem_provider(provider)
    if dem:
        # DEM 是墨卡托 XYZ 网格,列行数与 4326 不同,不能用同一套换算
        from ..core.dem_tiling import suggest_dem_levels
        return await asyncio.to_thread(
            suggest_dem_levels, (west, south, east, north),
            DEM_LAYERS[provider][2])
    return await asyncio.to_thread(
        suggest_levels, (west, south, east, north), 18, 1)


@router.get("/dem_max_level")
async def api_dem_max_level(west: float, south: float, east: float, north: float,
                            provider: str = "esri_terrain"):
    """探测 DEM 数据源在该范围的最高可用级别(有真实数据的最大 LOD)。

    Esri Terrain3D 各区域最高级别不同(超出返回空瓦片),前端据此禁用超限级别。
    """
    if not is_dem_provider(provider):
        raise HTTPException(400, "该数据源不支持地形级别探测")
    import asyncio

    from ..providers.terrain import probe_max_level
    # 探测是阻塞网络请求,放线程池避免阻塞事件循环
    max_level = await asyncio.to_thread(
        probe_max_level, (west, south, east, north), provider)
    return {"provider": provider, "max_level": max_level,
            "service_max": DEM_LAYERS[provider][2]}


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

    # 影像 / 高程每级 GeoTIFF:按文件名前缀归类(排除 dem/hillshade 前缀避免重复计入)
    geotiff_bytes = 0
    dem_bytes = 0
    for f in out_dir.glob(f"{name}_*.tif"):
        try:
            sz = f.stat().st_size
        except OSError:
            continue
        stem = f.name
        if stem == f"{name}_dem.tif":
            # 三维建筑任务的地面高程,下面单独归类,不计入影像 GeoTIFF
            continue
        if stem.startswith(f"{name}_dem_z") or stem.startswith(f"{name}_hillshade_z"):
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
    add("b3dm", "三维建筑 3D Tiles", _dir_size(out_dir / "3dtiles"))
    # 三维建筑的另两类成果:建筑轮廓矢量与地面高程
    _vec = out_dir / f"{name}_buildings.geojson"
    add("bld_vector", "建筑轮廓矢量", _vec.stat().st_size if _vec.exists() else 0)
    _bdem = out_dir / f"{name}_dem.tif"
    add("bld_dem", "地面高程 GeoTIFF", _bdem.stat().st_size if _bdem.exists() else 0)

    total = sum(it["bytes"] for it in items)
    return {"items": items, "total_bytes": total, "output_path": out}


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
    # DEM 走 AWS 公开数据集,无需天地图密钥;天地图数据源才校验密钥
    # (密钥来源:tk 使用池 或 config.yaml 的固定密钥)
    if not dem and not settings.tianditu.token and not token_pool.has_any():
        raise HTTPException(400, "未配置天地图密钥,请在密钥管理中添加,或在 config.yaml 填写 tianditu.token")

    levels = data.level_list()
    if not levels:
        raise HTTPException(400, "请至少选择一个下载级别")

    west, south, east, north = data.bbox
    detail = _estimate_detail((west, south, east, north), levels, data.provider)
    total = detail["total_tiles"]
    if total == 0:
        raise HTTPException(400, "所选范围在该级别下没有瓦片,请检查范围或级别")
    # 预估原始瓦片下载量(字节):仅下载量,非成果大小
    est_bytes = detail["total_bytes"]
    # 叠加注记时需额外下载同网格的注记瓦片,总数翻倍(保持进度准确)。DEM 无注记。
    if data.annotate and not dem:
        total *= 2
        est_bytes *= 2

    task_id = create_task(data, total, est_bytes)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": total, "status": "pending"}


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
    if not p.is_absolute() or not p.is_file():
        raise HTTPException(400, f"源文件不存在或不是绝对路径:{p}")

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
    total = _estimate_total((west, south, east, north), levels, data.provider)
    if total == 0:
        raise HTTPException(400, "所选范围在该级别下没有瓦片,请检查范围或级别")
    if data.annotate and not is_dem_provider(data.provider):
        total *= 2

    # 预估原始瓦片下载量(仅下载体积,非成果大小)
    est = _estimate_detail((west, south, east, north), levels, data.provider)
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
        hillshade=json.dumps(data.hillshade.model_dump()),
        stages=json.dumps(stages),
        est_bytes=est_bytes,
        total=total, downloaded=0, failed=0,
        status="pending", message="已重新加入队列",
    )
    task_queue.clear_control(task_id)
    await task_queue.enqueue(task_id)
    return {"id": task_id, "total": total, "status": "pending"}


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
