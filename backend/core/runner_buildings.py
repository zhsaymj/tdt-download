"""三维建筑白模任务执行:建筑轮廓 → 白模 → b3dm 3D Tiles。

阶段:fetch_buildings → base_dem(仅 terrain 模式) → build_mesh → tile_3d

**成果**(任务输出目录内,均保留):
    3dtiles/tileset.json + tiles/*.b3dm   Cesium 可加载的三维白模
    {任务名}_buildings.geojson            建筑轮廓矢量(含高度/底面高/属性)
    {任务名}_dem.tif                      地面高程 GeoTIFF(terrain 模式才有)
    metadata.json / _buildings_stats.json 数据说明与清洗统计

**中间文件与可恢复性**:建筑要素是内存对象,若只在内存中传递,任务暂停后
恢复时已完成阶段被跳过、数据却已丢失。故每阶段把结果落盘(与栅格管线保留
.osm_src.tif 同一思路),任一阶段都能独立重跑:

    _buildings_raw.jsonl       fetch 产物:原始轮廓 + 高度属性
    {任务名}_dem.tif           base_dem 产物:既是采样源,也是成果之一
    _buildings_prepared.jsonl  build_mesh 产物:已清洗/补高/定底面高

全部阶段成功后只删两个 .jsonl 中间件;DEM 与矢量是成果,不删。
"""
from __future__ import annotations

import json
from pathlib import Path

from ..config import settings
from ..models import get_task, update_task
from ..providers.buildings import BuildingFeature
from ..providers.buildings import build_building_source
from ..providers.cached import wrap_with_cache
from ..providers.terrain import build_terrain_provider
from .buildings import BaseHeightMode, prepare_buildings
from .dem import mosaic_dem_geotiff
from .dem_tiling import mercator_range_for_bbox
from .downloader import TileDownloader
from .logs import logger
from .progress import StageTracker
from .queue import task_queue
from .tileset3d import export_tileset

# 底面高采样用 DEM 的级别选取:建筑定位不需要高精度地形,
# 取满足"瓦片数不超过 DEM_TILE_BUDGET"的最大级别,上限 DEM_MAX_LEVEL。
DEM_MAX_LEVEL = 13
DEM_MIN_LEVEL = 6
DEM_TILE_BUDGET = 480


class _Stopped(Exception):
    """被暂停/取消,跳回顶层落库。"""


# ---------- 成果文件命名(集中定义,避免散落各处不一致)----------

def dem_output(out_dir: Path, task_name: str) -> Path:
    """地面高程 GeoTIFF。既作为底面高采样源,也是成果之一,故用正式命名。"""
    return out_dir / f"{task_name}_dem.tif"


def vector_output(out_dir: Path, task_name: str) -> Path:
    """建筑轮廓矢量成果(GeoJSON,WGS84)。"""
    return out_dir / f"{task_name}_buildings.geojson"


def tiles3d_output(out_dir: Path) -> Path:
    return out_dir / "3dtiles"


def _pick_dem_level(bbox) -> int:
    """为底面高采样挑一个够用又不过量的 DEM 级别。"""
    for z in range(DEM_MAX_LEVEL, DEM_MIN_LEVEL - 1, -1):
        if mercator_range_for_bbox(*bbox, z).count <= DEM_TILE_BUDGET:
            return z
    return DEM_MIN_LEVEL


# ---------- 中间文件读写 ----------

def _dump_features(path: Path, feats, should_stop=None) -> tuple[int, bool]:
    """把建筑要素写成 JSONL(一行一栋)。返回 (条数, 是否被中断)。

    先写 .tmp 再原子改名。**被中断时绝不改名**——否则残缺文件会被下次
    恢复当成完整产物复用,导致建筑莫名缺失。
    """
    n = 0
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for ft in feats:
            f.write(json.dumps({
                "fid": ft.fid,
                "rings": ft.rings,
                "height": ft.height,
                "num_floors": ft.num_floors,
                "name": ft.name,
                "height_source": ft.height_source,
                "base_height": ft.base_height,
                "props": ft.props,
            }, ensure_ascii=False) + "\n")
            n += 1
    # 中断则丢弃半成品,不改名(下次从头重来,而非复用残缺数据)
    if should_stop and should_stop():
        tmp.unlink(missing_ok=True)
        return n, True
    tmp.replace(path)
    return n, False


def _load_features(path: Path):
    """流式读回 JSONL 建筑要素。"""
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            yield BuildingFeature(
                fid=d.get("fid", ""),
                rings=[[(float(x), float(y)) for x, y in ring] for ring in d["rings"]],
                height=d.get("height"),
                num_floors=d.get("num_floors"),
                name=d.get("name", ""),
                height_source=d.get("height_source", ""),
                base_height=float(d.get("base_height") or 0.0),
                props=d.get("props") or {},
            )


def _load_vector(path: Path):
    """从建筑轮廓矢量成果读回要素(切片阶段的回退数据源)。

    任务完成后 _buildings_prepared.jsonl 会被清理,此时若用户对已完成任务
    单独重跑「切 3D Tiles」,没有这个回退就会报"缺少建模中间文件"。
    矢量成果里已含切片所需全部信息(轮廓/高度/底面高/属性),正好可以顶上,
    不必为此多留一份中间件。
    """
    gj = json.loads(path.read_text(encoding="utf-8"))
    for feat in gj.get("features") or []:
        geom = feat.get("geometry") or {}
        if geom.get("type") != "Polygon":
            continue
        rings = []
        for ring in geom.get("coordinates") or []:
            pts = [(float(c[0]), float(c[1])) for c in ring or [] if c and len(c) >= 2]
            # 去掉 GeoJSON 的闭合末点(内部约定环不闭合)
            if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-12 \
                    and abs(pts[0][1] - pts[-1][1]) < 1e-12:
                pts = pts[:-1]
            if len(pts) >= 3:
                rings.append(pts)
        if not rings:
            continue
        p = feat.get("properties") or {}
        # 除固定字段外的都是用户自选保留字段,原样带回 props
        fixed = {"id", "name", "height", "base_height", "top_height", "height_src"}
        props = {k: v for k, v in p.items() if k not in fixed}
        yield BuildingFeature(
            fid=str(p.get("id") or ""),
            rings=rings,
            height=float(p.get("height") or 0.0),
            name=p.get("name") or "",
            height_source=p.get("height_src") or "",
            base_height=float(p.get("base_height") or 0.0),
            props=props,
        )


def _write_vector(path: Path, feats) -> int:
    """把建筑要素写成 GeoJSON 成果(WGS84),返回条数。

    属性用可读字段名(而非内部简写),便于在 QGIS/ArcGIS 里直接查看核对:
      height       最终采用的建筑高度(米,已补全)
      base_height  底面海拔(米,terrain 模式为 DEM 采样值)
      top_height   顶面海拔 = base_height + height
      height_src   高度来源:height/num_floors/area_estimate/default
      area_m2      占地面积
    以及用户在本地矢量上传时选择保留的原始字段(原名保留)。

    流式写(逐要素追加)而非先构造整个 dict:18 万栋的 GeoJSON 有几百 MB,
    全量拼在内存里没必要。
    """
    tmp = path.with_suffix(path.suffix + ".tmp")
    n = 0
    with tmp.open("w", encoding="utf-8") as f:
        f.write('{"type":"FeatureCollection","features":[\n')
        for ft in feats:
            base = float(ft.base_height or 0.0)
            h = float(ft.height or 0.0)
            props = {
                "id": ft.fid,
                "name": ft.name or "",
                "height": round(h, 2),
                "base_height": round(base, 2),
                "top_height": round(base + h, 2),
                "height_src": ft.height_source or "",
            }
            for k, v in (ft.props or {}).items():
                # area_m2 用规范名暴露;其余是用户自选保留的原始字段,保持原名
                props.setdefault("area_m2" if k == "area_m2" else k, v)
            # 环写成闭合(GeoJSON 规范要求首尾相同)
            coords = [[[round(x, 7), round(y, 7)] for x, y in ring + [ring[0]]]
                      for ring in ft.rings]
            if n:
                f.write(",\n")
            json.dump({"type": "Feature", "properties": props,
                       "geometry": {"type": "Polygon", "coordinates": coords}},
                      f, ensure_ascii=False)
            n += 1
        f.write("\n]}\n")
    tmp.replace(path)
    return n


def _to_selected_container(task: dict, geojson_path: Path) -> Path:
    """按任务选定的容器,把已写好的 GeoJSON 轮廓另存为 GPKG / Shapefile。

    为什么先写 GeoJSON 再转、而不直接按目标格式写:_write_vector 是**流式**写出的
    (18 万栋的成果几百 MB,全量拼在内存里没必要),而 pyogrio 的写入要求一次给全
    要素数组。先落 GeoJSON 保住流式优势,再由 GDAL 做格式转换,内存占用由它控制。

    转换失败时保留 GeoJSON 并返回它——成果格式不对可以重导,成果丢了不能。
    """
    from .containers import container_of
    from .formats import CONTAINERS

    container = container_of(task, "fetch_buildings")
    if container in ("", "geojson") or not geojson_path.exists():
        return geojson_path
    cont = CONTAINERS.get(container)
    if cont is None or cont.writer != "pyogrio":
        return geojson_path

    # 走 pyogrio.raw 而非 read_dataframe:后者需要 geopandas,项目没装(也不想装,
    # 它会拖进 pandas 全家桶)。raw 接口返回 (meta, index, geometry, field_data),
    # 字段名在 meta["fields"]、字段值在第四项 —— 与 write 的入参顺序正好对得上。
    import numpy as _np
    from pyogrio.raw import read as _read, write as _write

    out_path = geojson_path.with_suffix(cont.ext)
    try:
        meta, _idx, geom, field_data = _read(str(geojson_path))
        _write(str(out_path), geom, list(field_data), _np.asarray(meta["fields"]),
               driver=cont.driver, crs=meta.get("crs") or "EPSG:4326",
               geometry_type="Polygon", encoding="UTF-8",
               promote_to_multi=True)
        logger.info("建筑轮廓已转为 %s:%s(%d 栋)",
                    cont.label, out_path.name, len(geom))
        # Shapefile 字段名上限 10 字符,超长会被 GDAL 静默截断
        # (实测 base_height → base_heigh)。这是格式固有限制,记日志让用户可查。
        if container == "shapefile":
            long_names = [str(x) for x in meta["fields"] if len(str(x)) > 10]
            if long_names:
                logger.warning(
                    "Shapefile 字段名上限 10 字符,以下字段已被截断:%s"
                    "(需完整字段名请改用 GeoPackage)", ", ".join(long_names))
        geojson_path.unlink(missing_ok=True)
        return out_path
    except Exception as e:
        logger.warning("建筑轮廓转 %s 失败:%s,保留 GeoJSON", cont.label, e)
        out_path.unlink(missing_ok=True)
        return geojson_path


def _fmt_size(p: Path) -> str:
    try:
        n = float(p.stat().st_size)
    except OSError:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ---------- 各阶段 ----------

def _stage_fetch(ctx) -> list[str]:
    """从 Overture 按 bbox 拉建筑轮廓,落 _buildings_raw.jsonl。"""
    raw_path = ctx.out_dir / "_buildings_raw.jsonl"
    if raw_path.exists() and raw_path.stat().st_size > 0:
        logger.info("任务[%s] 复用已拉取的建筑轮廓,跳过取数", ctx.task["name"])
        ctx.tracker.update("fetch_buildings", message="复用已拉取的轮廓")
        return []

    ctx.tracker.start("fetch_buildings", total=0, message="连接 Overture 数据源")
    src = ctx.source

    # Overture 需先确定发布版本(要联网、可能失败,且失败信息最具指导性),
    # 显式提前触发,避免被后面的"计数失败"warning 盖住、误导排查方向。
    # Overpass 无版本概念,跳过。
    # 按 provider 显式判断而非 hasattr:release 属性内部会联网探测,
    # 放在 hasattr 里会让一句条件判断产生网络副作用、出错位置也难读。
    if ctx.task["provider"] == "overture_buildings":
        ctx.tracker.update("fetch_buildings", message="确定数据版本")
        release = src.release      # 属性内部做探测,失败抛带处置建议的 RuntimeError
        logger.info("任务[%s] 数据源 %s 版本 %s", ctx.task["name"], src.key, release)
        ctx.tracker.update("fetch_buildings", message=f"数据版本 {release}")
    else:
        logger.info("任务[%s] 数据源 %s", ctx.task["name"], src.key)

    # 先取个数做进度分母(聚合查询比拉全字段便宜);失败则以 0 分母跑,只报数量
    total = 0
    try:
        total = src.count(ctx.bbox)
        logger.info("任务[%s] 范围内建筑约 %d 栋", ctx.task["name"], total)
    except Exception as e:
        logger.warning("建筑计数失败,进度以未知总数运行:%s", e)
    ctx.tracker.update("fetch_buildings", done=0, total=total,
                       message=f"开始拉取(约 {total} 栋)" if total else "开始拉取")

    def on_progress(fetched, _total):
        _check_stop(ctx)
        ctx.tracker.update("fetch_buildings", done=fetched,
                           message=f"已拉取 {fetched} 栋")

    feats = src.fetch(ctx.bbox, on_progress=on_progress, should_stop=ctx.should_stop)
    n, stopped = _dump_features(raw_path, feats, ctx.should_stop)
    if stopped:
        raise _Stopped()
    if n == 0:
        raise RuntimeError("该范围内没有建筑数据(Overture 在此区域可能无覆盖)")
    ctx.tracker.update("fetch_buildings", done=n, total=max(n, total),
                       message=f"共 {n} 栋")
    return []


def _stage_base_dem(ctx) -> list[str]:
    """准备地面高程:用于逐栋采样底面高,同时作为成果保留。

    高程源优先级:
      1. 用户上传的本地地形(更精细)
      2. 在线地形(Esri Terrain3D)——仅在上传地形未完全覆盖范围时才下载,
         用于兜底,避免范围外的建筑取不到高程

    上传地形已完全覆盖下载范围时不再下载在线地形(省时间也省流量)。
    """
    task = ctx.task
    dem_path = dem_output(ctx.out_dir, task["name"])

    # ---- 上传地形:检查覆盖情况 ----
    up_id = (task.get("dem_upload_id") or "").strip()
    up_path = None
    if up_id:
        from .dem_upload import coverage_ratio, meta_of, path_of
        try:
            up_path = path_of(up_id)
        except FileNotFoundError as e:
            raise RuntimeError(str(e)) from e
        meta = meta_of(up_id) or {}
        ratio = coverage_ratio(meta.get("bounds_wgs84"), ctx.bbox)
        ctx.dem_upload_ratio = ratio
        logger.info("任务[%s] 上传地形 %s(%s,%.1f 米分辨率)覆盖所选范围 %.1f%%",
                    task["name"], meta.get("filename") or up_id,
                    meta.get("crs"), meta.get("res_m_approx") or 0.0, ratio * 100)
        if ratio >= 0.999:
            # 完全覆盖:不必再下在线地形
            ctx.tracker.start("base_dem", total=1,
                              message=f"使用上传地形(覆盖 {ratio * 100:.0f}%)")
            ctx.tracker.update("base_dem", done=1, message="上传地形就绪")
            return [str(up_path)]
        # 不在此 start:下面下载在线地形时会 start(total=1000),重复调用会覆盖消息
        logger.info("任务[%s] 上传地形未完全覆盖(%.1f%%),将下载在线地形作兜底",
                    task["name"], ratio * 100)

    # ---- 在线地形(无上传 或 上传未盖全)----
    if dem_path.exists() and dem_path.stat().st_size > 0:
        logger.info("任务[%s] 复用已备在线地面高程", task["name"])
        ctx.tracker.update("base_dem", message="复用已备高程")
        return [str(dem_path)]

    z = _pick_dem_level(ctx.bbox)
    tr = mercator_range_for_bbox(*ctx.bbox, z)
    logger.info("任务[%s] 底面高程:自动下载 DEM 第 %d 级,共 %d 张瓦片",
                ctx.task["name"], z, tr.count)

    provider = build_terrain_provider("esri_terrain")
    downloader = TileDownloader(
        provider,
        cache_dir=settings.cache_dir,
        concurrency=settings.download.concurrency,
        max_retries=settings.download.max_retries,
        timeout=settings.download.timeout,
        use_cache=True,     # 底面高程恒复用缓存(与成果无关,不必强制重下)
    )

    # 下载占前 70% 刻度,拼接占后 30%(用 0-1000 内部刻度统一映射)
    ctx.tracker.start("base_dem", total=1000, message=f"下载高程瓦片(第 {z} 级)")
    got = 0

    def on_dl(ok_delta, fail_delta):
        nonlocal got
        got += ok_delta + fail_delta
        ctx.tracker.update("base_dem",
                           done=int(700 * min(got / max(tr.count, 1), 1.0)),
                           message=f"下载高程瓦片 {got}/{tr.count}")

    import asyncio
    _ok, _fail, stopped = asyncio.run(
        downloader.download_range(tr, on_dl, ctx.should_stop))
    if stopped:
        raise _Stopped()

    def on_row(done_rows, total_rows):
        _check_stop(ctx)
        ctx.tracker.update("base_dem",
                           done=700 + int(300 * done_rows / max(total_rows, 1)),
                           message=f"拼接高程({done_rows}/{total_rows} 行)")

    tmp = ctx.out_dir / "_base_dem.tmp.tif"
    # 成果 DEM 建金字塔:它会被 GIS 直接打开查看,有概览图浏览才流畅
    mosaic_dem_geotiff(tr, tmp, downloader.tile_path,
                       build_overviews=True, on_row=on_row)
    tmp.replace(dem_path)
    ctx.tracker.update("base_dem", done=1000,
                       message=f"高程就绪({_fmt_size(dem_path)})")
    return [str(dem_path)]


def _stage_build_mesh(ctx) -> list[str]:
    """清洗轮廓 + 补全高度 + 定底面高。

    产出中间件 _buildings_prepared.jsonl(供切片阶段与断点续跑),
    以及成果 {任务名}_buildings.geojson(建筑轮廓矢量)。
    """
    task = ctx.task
    raw_path = ctx.out_dir / "_buildings_raw.jsonl"
    prep_path = ctx.out_dir / "_buildings_prepared.jsonl"
    vec_path = vector_output(ctx.out_dir, task["name"])
    if not (raw_path.exists() and raw_path.stat().st_size > 0):
        raise RuntimeError("缺少建筑轮廓中间文件,请重跑「拉取建筑轮廓」阶段")

    if prep_path.exists() and prep_path.stat().st_size > 0:
        logger.info("任务[%s] 复用已建模建筑数据", task["name"])
        ctx.tracker.update("build_mesh", message="复用已建模数据")
        # 中间件在但矢量成果缺失(如旧任务续跑):补出矢量
        if not vec_path.exists():
            n = _write_vector(vec_path, _load_features(prep_path))
            logger.info("任务[%s] 补出建筑轮廓矢量:%d 栋", task["name"], n)
            return [str(_to_selected_container(task, vec_path))]
        return [str(vec_path)]

    mode = task.get("base_height_mode") or BaseHeightMode.TERRAIN
    # 高程源按优先级:上传地形(更精细)→ 在线地形(兜底,仅上传未盖全时才存在)
    dem_sources = []
    if mode == BaseHeightMode.TERRAIN:
        up_id = (task.get("dem_upload_id") or "").strip()
        if up_id:
            from .dem_upload import path_of as _dem_up_path
            try:
                dem_sources.append((_dem_up_path(up_id), "上传地形"))
            except FileNotFoundError as e:
                raise RuntimeError(str(e)) from e
        online = dem_output(ctx.out_dir, task["name"])
        if online.exists():
            dem_sources.append((online, "在线地形"))

    # 用原始行数当分母,进度才有意义
    with raw_path.open("r", encoding="utf-8") as f:
        raw_total = sum(1 for line in f if line.strip())
    ctx.tracker.start("build_mesh", total=raw_total, message="清洗轮廓并计算底面高")

    def on_progress(kept, _t):
        _check_stop(ctx)
        ctx.tracker.update("build_mesh", done=kept, message=f"已处理 {kept} 栋")

    feats, stats = prepare_buildings(
        _load_features(raw_path),
        base_mode=mode,
        dem_sources=dem_sources,
        height_offset=float(task.get("height_offset") or 0.0),
        default_height=float(task.get("default_height") or 6.0),
        on_progress=on_progress,
        should_stop=ctx.should_stop,
    )
    # prepare_buildings 遇停止会提前返回部分结果,这里先判定再落盘
    if ctx.should_stop():
        raise _Stopped()
    if not feats:
        raise RuntimeError("清洗后没有可用建筑(轮廓可能全部无效或过小)")

    n, stopped = _dump_features(prep_path, feats, ctx.should_stop)
    if stopped:
        raise _Stopped()
    ctx.stats = stats
    (ctx.out_dir / "_buildings_stats.json").write_text(
        json.dumps(stats.as_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    update_task(ctx.task["id"], building_count=n)

    # 建筑轮廓矢量成果:带最终高度/底面高/属性,可直接在 GIS 里打开核对
    ctx.tracker.update("build_mesh", message="导出建筑轮廓矢量")
    nv = _write_vector(vec_path, iter(feats))
    logger.info("任务[%s] 建筑轮廓矢量:%d 栋 → %s", task["name"], nv, vec_path.name)
    final_vec = _to_selected_container(task, vec_path)

    ctx.tracker.update("build_mesh", done=raw_total, total=raw_total,
                       message=f"可用建筑 {n} 栋")
    return [str(final_vec)]


def _stage_tile_3d(ctx) -> list[str]:
    """四叉树分级并写出 b3dm + tileset.json。

    数据来源优先用建模中间件;它在任务完成后会被清理,那时改读矢量成果
    (内容等价),这样对已完成任务单独重跑本阶段也能成功。
    """
    prep_path = ctx.out_dir / "_buildings_prepared.jsonl"
    vec_path = vector_output(ctx.out_dir, ctx.task["name"])
    ctx.tracker.start("tile_3d", total=1, message="加载建模数据")
    if prep_path.exists() and prep_path.stat().st_size > 0:
        feats = list(_load_features(prep_path))
    elif vec_path.exists() and vec_path.stat().st_size > 0:
        logger.info("任务[%s] 建模中间件已清理,改用矢量成果作切片输入",
                    ctx.task["name"])
        ctx.tracker.update("tile_3d", message="读取建筑轮廓矢量")
        feats = list(_load_vector(vec_path))
    else:
        raise RuntimeError("缺少建模数据,请重跑「建筑白模建模」阶段")
    if not feats:
        raise RuntimeError("建模数据为空")

    def on_progress(done, total):
        _check_stop(ctx)
        ctx.tracker.update("tile_3d", done=done, total=total,
                           message=f"写出瓦片 {done}/{total}")

    tiles_root = ctx.out_dir / "3dtiles"
    path, written, stopped = export_tileset(
        feats, tiles_root,
        max_per_tile=int(ctx.task.get("max_per_tile") or 2000),
        on_progress=on_progress, should_stop=ctx.should_stop,
    )
    if stopped:
        raise _Stopped()      # 已写瓦片保留,恢复时续写
    ctx.tracker.update("tile_3d", message=f"共 {written} 个 b3dm")
    return [str(tiles_root)]


def _check_stop(ctx):
    if ctx.should_stop():
        raise _Stopped()


# ---------- 顶层调度 ----------

_EXECUTORS = {
    "fetch_buildings": _stage_fetch,
    "base_dem": _stage_base_dem,
    "build_mesh": _stage_build_mesh,
    "tile_3d": _stage_tile_3d,
}


class _Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def run_buildings_task(task_id: str, emit) -> None:
    """三维建筑白模任务主流程。由 runner.run_task 分派进来。"""
    import asyncio

    task = get_task(task_id)
    if not task:
        return

    bcfg = getattr(settings, "buildings", None)
    src_kw: dict = {}
    if task["provider"] == "local_vector":
        # 本地矢量面:把用户选的字段映射传给数据源
        src_kw.update(
            upload_id=task.get("upload_id") or "",
            height_field=task.get("height_field") or "",
            height_mode=task.get("height_mode") or "none",
            height_scale=float(task.get("height_scale") or 1.0),
            floor_height=float(task.get("floor_height") or 3.0),
            name_field=task.get("name_field") or "",
            keep_fields=task.get("keep_fields") or [],
        )
    if bcfg is not None and task["provider"] == "osm_buildings":
        mirrors = bcfg.mirror_list() if hasattr(bcfg, "mirror_list") else None
        if mirrors:
            src_kw["mirrors"] = mirrors
        chunk = getattr(bcfg, "overpass_chunk_deg", None)
        if chunk:
            src_kw["max_chunk_deg"] = float(chunk)
    source = build_building_source(
        task["provider"],
        release=getattr(bcfg, "release", None) if bcfg else None,
        proxy=getattr(bcfg, "proxy", None) if bcfg else None,
        **src_kw,
    )
    # 套网格缓存:按 0.05° 格子逐格缓存,缺格才联网。
    # use_cache=False(界面「不使用缓存」)时强制重抓,但仍回写缓存供后续任务复用。
    # 本地上传的矢量面不套(数据已在本地),wrap_with_cache 内部会跳过。
    source = wrap_with_cache(
        source,
        use_cache=bool(task.get("use_cache", True)),
        ttl_days=float(getattr(bcfg, "cache_ttl_days", 30.0) if bcfg else 30.0),
    )

    out_dir = Path(task["output_path"]) if task.get("output_path") else settings.output_dir / task_id
    out_dir.mkdir(parents=True, exist_ok=True)
    west, south, east, north = task["bbox"]

    tracker = StageTracker(task_id, task.get("stages") or [], emit,
                           task_name=task.get("name") or task_id)

    def should_stop() -> bool:
        return task_queue.control_of(task_id) in ("pause", "cancel")

    ctx = _Ctx(task=task, source=source, out_dir=out_dir,
               bbox=(west, south, east, north), tracker=tracker,
               should_stop=should_stop, stats=None)

    update_task(task_id, status="running", message="开始拉取建筑数据")
    emit({"type": "task", "id": task_id, "status": "running"})
    logger.info("任务[%s]开始运行(三维建筑) 底面高模式=%s 待执行阶段=%s",
                task["name"], task.get("base_height_mode"), tracker.pending_keys())

    outputs: list[str] = []
    any_failed = False
    for stage in list(tracker.stages):
        key = stage["key"]
        if key not in _EXECUTORS or tracker.is_done(key):
            continue
        label = stage.get("label", key)
        if stage.get("reset"):
            _clear_stage_output(ctx, key)
            stage.pop("reset", None)
        logger.info("任务[%s] 阶段[%s] 开始", task["name"], label)
        import time as _t
        t0 = _t.time()
        try:
            produced = await asyncio.to_thread(_EXECUTORS[key], ctx)
            outputs.extend(produced or [])
            tracker.finish(key)
            logger.info("任务[%s] 阶段[%s] 完成,用时 %.1fs", task["name"], label, _t.time() - t0)
        except (_Stopped, InterruptedError):
            # InterruptedError 来自数据源内部对取消标志的响应(Overpass 重试间隙、
            # Overture 的 DuckDB interrupt),语义同 _Stopped
            logger.info("任务[%s] 阶段[%s] 被暂停/取消", task["name"], label)
            return _handle_stop(task_id, tracker, key, emit)
        except Exception as e:
            any_failed = True
            tracker.fail(key, message=str(e)[:200])
            logger.exception("任务[%s] 阶段[%s] 失败:%s", task["name"], label, e)
            break      # 建筑管线各阶段严格依赖前序,失败后续无法进行

    task = get_task(task_id) or task
    count = int(task.get("building_count") or 0)

    # 按磁盘实际情况汇总成果(单阶段重试时 outputs 只含本轮产物,
    # 这里重建完整清单,避免漏列已有成果)
    outputs = _collect_outputs(out_dir, task["name"])

    try:
        _write_metadata(ctx, outputs, count)
    except Exception as e:
        logger.warning("任务[%s] 写 metadata.json 失败:%s", task["name"], e)

    if any_failed:
        msg = "部分失败(可单独重试失败阶段)"
        update_task(task_id, status="failed", message=msg, output_path=str(out_dir))
        emit({"type": "task", "id": task_id, "status": "failed",
              "message": msg, "output_path": str(out_dir), "outputs": outputs})
    else:
        _cleanup_intermediates(out_dir)
        msg = f"完成:{count} 栋建筑"
        update_task(task_id, status="done", message=msg, output_path=str(out_dir),
                    downloaded=count, total=max(count, 1))
        emit({"type": "task", "id": task_id, "status": "done",
              "message": msg, "output_path": str(out_dir), "outputs": outputs})
    logger.info("任务[%s] 三维建筑流程结束,%s", task["name"],
                "有阶段失败" if any_failed else "成功")


def _handle_stop(task_id, tracker, key, emit):
    ctrl = task_queue.control_of(task_id)
    if ctrl == "cancel":
        tracker.pause(key, message="已取消")
        update_task(task_id, status="canceled", message="已取消")
        emit({"type": "task", "id": task_id, "status": "canceled", "message": "已取消"})
    else:
        tracker.pause(key, message="已暂停")
        update_task(task_id, status="paused", message="已暂停")
        emit({"type": "task", "id": task_id, "status": "paused", "message": "已暂停"})


def _clear_stage_output(ctx, key: str) -> None:
    """清空某阶段旧产出(单阶段重试/改参数重来时)。

    注意下游依赖:重跑取数或建模,必须连带清掉其下游中间文件,
    否则下游会复用与新参数不匹配的旧数据。
    """
    import shutil as _sh
    out = ctx.out_dir

    def rm(p: Path):
        try:
            if p.is_dir():
                _sh.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("清理阶段产出失败 %s:%s", p, e)

    name = ctx.task["name"]
    # 原则:只清"中间件"与"会累积文件的目录",**不预删成果文件**。
    # 成果(矢量/DEM)都是先写 .tmp 再原子改名,阶段成功时自然覆盖旧值;
    # 提前删掉只会在阶段失败时白丢成果——曾因此出现"重跑建模失败后矢量也没了"。
    # 例外:base_dem 重跑必须删 DEM,否则该阶段会判定"已有高程"直接复用、
    # 重新下载就无从发生;而 DEM 本身可重新下载,风险可接受。
    if key == "fetch_buildings":
        rm(out / "_buildings_raw.jsonl")
        rm(out / "_buildings_raw.jsonl.tmp")
        rm(out / "_buildings_prepared.jsonl")     # 下游失效
        rm(tiles3d_output(out))
    elif key == "base_dem":
        rm(dem_output(out, name))                 # 见上:必须删才会重新下载
        rm(out / "_base_dem.tmp.tif")
        rm(out / "_buildings_prepared.jsonl")     # 底面高变了,建模需重做
        rm(tiles3d_output(out))
    elif key == "build_mesh":
        rm(out / "_buildings_prepared.jsonl")
        rm(tiles3d_output(out))
    elif key == "tile_3d":
        rm(tiles3d_output(out))
    logger.info("任务[%s] 阶段[%s] 旧产出已清空(重来)", name, key)


def _uploaded_dem_info(task: dict, ctx) -> dict | None:
    """上传地形的说明(写入 metadata);未使用上传地形则返回 None。"""
    up_id = (task.get("dem_upload_id") or "").strip()
    if not up_id:
        return None
    from .dem_upload import meta_of
    m = meta_of(up_id) or {}
    ratio = getattr(ctx, "dem_upload_ratio", None)
    return {
        "dem_id": up_id,
        "filename": m.get("filename"),
        "crs": m.get("crs"),
        "resolution_m_approx": m.get("res_m_approx"),
        "bounds_wgs84": m.get("bounds_wgs84"),
        "coverage_of_range": (round(ratio, 4) if ratio is not None else None),
        "note": ("上传地形优先用于底面高采样;其未覆盖处回落在线地形兜底。"
                 "两种高程源基准可能不同,交界处可能出现台阶"),
    }


def _collect_outputs(out_dir: Path, task_name: str) -> list[str]:
    """扫盘汇总当前全部成果路径(供 metadata 与前端展示)。"""
    outs: list[str] = []
    for p in (tiles3d_output(out_dir),
              vector_output(out_dir, task_name),
              dem_output(out_dir, task_name)):
        if p.exists():
            outs.append(str(p))
    return outs


def _cleanup_intermediates(out_dir: Path) -> None:
    """全部成功后清理中间文件。

    只删两个 .jsonl 及其临时件——DEM 与建筑轮廓矢量都是成果,必须保留。
    """
    for name in ("_buildings_raw.jsonl", "_buildings_prepared.jsonl",
                 "_buildings_raw.jsonl.tmp", "_buildings_prepared.jsonl.tmp",
                 "_base_dem.tmp.tif"):
        p = out_dir / name
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("中间文件暂时无法删除(将于下次清理):%s(%s)", p, e)


def _write_metadata(ctx, outputs, count: int) -> None:
    """写三维建筑成果的 metadata.json(结构与栅格管线保持一致的字段风格)。"""
    from datetime import datetime

    task = ctx.task
    west, south, east, north = ctx.bbox
    stats_path = ctx.out_dir / "_buildings_stats.json"
    stats = None
    if stats_path.exists():
        try:
            stats = json.loads(stats_path.read_text(encoding="utf-8"))
        except Exception:
            stats = None

    meta = {
        "task_id": task["id"],
        "name": task["name"],
        "provider": task["provider"],
        "provider_name": ctx.source.label,
        "data_type": "buildings_3dtiles",
        "tile_format": "b3dm (3D Tiles 1.0)",
        "bbox_wgs84": [west, south, east, north],
        "output_crs": "EPSG:4979 (WGS84 3D, ECEF geometry in b3dm)",
        "building_count": count,
        # 三类成果各自的格式与坐标系(三者坐标系不同,这里必须写清)
        "products": {
            "tiles_3d": {
                "path": "3dtiles/tileset.json",
                "format": "3D Tiles 1.0 (b3dm)",
                "crs": "ECEF (b3dm 内为相对 RTC_CENTER 的局部坐标)",
                "note": "Cesium 用 Cesium3DTileset.fromUrl 直接加载",
            },
            "buildings_vector": {
                "path": vector_output(ctx.out_dir, task["name"]).name,
                "format": "GeoJSON (Polygon)",
                "crs": "EPSG:4326 (WGS84 经纬度)",
                "note": ("建筑轮廓面,属性含 height(最终高度)、base_height(底面海拔)、"
                         "top_height(顶面海拔)、height_src(高度来源)、area_m2"),
            },
            "ground_dem": ({
                "path": dem_output(ctx.out_dir, task["name"]).name,
                "format": "GeoTIFF (单波段 float32)",
                "crs": "EPSG:3857 (Web 墨卡托)",
                "value": "真实海拔(米),Esri Terrain3D LERC 解码",
                "note": "在线地形。上传地形未完全覆盖范围时才会生成,用于兜底",
            } if (task.get("base_height_mode") == "terrain"
                  and dem_output(ctx.out_dir, task["name"]).exists()) else None),
        },
        "base_height": {
            "mode": task.get("base_height_mode"),
            "offset_m": float(task.get("height_offset") or 0.0),
            # 上传地形的来源与覆盖情况(各源实际命中栋数见 clean_stats)
            "uploaded_dem": _uploaded_dem_info(task, ctx),
            "note": ("底面海拔由 DEM 逐栋采样烘焙进几何(Cesium3DTileset 不会自动贴地形);"
                     "与本工具导出的 terrain 切片同源、基准自洽"
                     if task.get("base_height_mode") == "terrain"
                     else "底面为固定高度,加载地形时可能穿模"),
        },
        "default_height_m": float(task.get("default_height") or 6.0),
        "max_buildings_per_tile": int(task.get("max_per_tile") or 2000),
        # 本地矢量面才有字段映射;其余数据源为 None
        "field_mapping": ({
            "upload_id": task.get("upload_id"),
            "height_field": task.get("height_field"),
            "height_mode": task.get("height_mode"),
            "height_scale": float(task.get("height_scale") or 1.0),
            "floor_height_m": float(task.get("floor_height") or 3.0),
            "name_field": task.get("name_field"),
            "kept_fields": task.get("keep_fields") or [],
        } if task["provider"] == "local_vector" else None),
        "clean_stats": stats,
        "outputs": [Path(p).name for p in outputs],
        "entry": "3dtiles/tileset.json",
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (ctx.out_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
