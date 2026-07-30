"""任务执行:阶段化管线(下载 → 合并 → 各切片格式),逐阶段上报进度。

被 TaskQueue 调用:async run_task(task_id, emit)。
emit(msg: dict) 把进度同步广播给 WebSocket 订阅者。

阶段模型(见 models.build_stage_defs):
  影像:download → geotiff → tms → osm
  DEM :download → dem(高程/晕渲) → tiles → terrain
各阶段相互独立:已完成(done)的阶段跳过(断点续跑 / 单阶段重跑),
单个导出阶段失败只标记该阶段 failed,不阻断其余阶段。任务终态由阶段汇总。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import settings
from ..models import get_task, parse_export, update_task
from ..providers.buildings import is_building_provider
from ..providers.terrain import build_terrain_provider, is_dem_provider
from ..providers.tianditu import build_annotation_provider, build_provider
from .dem import hillshade_from_dem, mosaic_dem_geotiff
from .dem_tiling import mercator_range_for_bbox
from .downloader import TileDownloader
from .logs import logger
from .metadata import write_metadata
from .mosaic import mosaic_to_geotiff
from .osm import export_osm
from .postprocess import clip_to_geometry, reproject_geotiff
from .progress import StageTracker
from .queue import task_queue
from .terrain_tiles import export_terrain, level_for_resolution, write_layer_json
from .token_pool import token_pool
from .tms import export_tms, write_tilemapresource
from .tiling import range_for_bbox

# OSM 金字塔起始下限:低于此级的超低层(单瓦片跨度巨大、小范围里基本全透明)
# 不生成。实际最低级 = min(OSM_MIN_LEVEL, 最高级),且 export_osm 会跳过无数据瓦片。
OSM_MIN_LEVEL = 3

# 被暂停/取消时抛出,用于从深层导出阶段快速跳回顶层做状态落库
class _Stopped(Exception):
    pass


def _safe_unlink(p: Path, retries: int = 5, delay: float = 0.3) -> None:
    """删除中间临时文件的收尾动作:Windows 下 GDAL 句柄可能尚未完全释放,
    unlink 会抛 WinError 32(文件被占用)。带几次重试;仍失败只记警告、
    绝不抛异常——临时文件删不掉不该让已切好的整个阶段被判失败(残留会在
    下次重试/清理时再删)。
    """
    import time as _time
    for i in range(retries):
        try:
            p.unlink(missing_ok=True)
            return
        except OSError:
            if i < retries - 1:
                _time.sleep(delay)
    logger.warning("临时文件暂时无法删除(将于下次清理):%s", p)


async def run_task(task_id: str, emit) -> None:
    task = get_task(task_id)
    if not task:
        return

    # 三维建筑白模走独立管线(数据是矢量要素集,无瓦片行列号,不复用下载器/拼接)
    if is_building_provider(task["provider"]):
        from .runner_buildings import run_buildings_task
        return await run_buildings_task(task_id, emit)

    is_dem = is_dem_provider(task["provider"])
    if is_dem:
        provider = build_terrain_provider(task["provider"])
    else:
        token_src = token_pool.use_token if token_pool.has_any() else settings.tianditu.token
        provider = build_provider(task["provider"], token_src)
    downloader = TileDownloader(
        provider,
        cache_dir=settings.cache_dir,
        concurrency=settings.download.concurrency,
        max_retries=settings.download.max_retries,
        timeout=settings.download.timeout,
        use_cache=task.get("use_cache", True),
    )

    annotate = task.get("annotate", False) and not is_dem
    anno_token_src = token_pool.use_token if token_pool.has_any() else settings.tianditu.token
    anno_provider = build_annotation_provider(task["provider"], anno_token_src) if annotate else None
    anno_downloader = TileDownloader(
        anno_provider,
        cache_dir=settings.cache_dir,
        concurrency=settings.download.concurrency,
        max_retries=settings.download.max_retries,
        timeout=settings.download.timeout,
        use_cache=task.get("use_cache", True),
    ) if anno_provider else None

    west, south, east, north = task["bbox"]
    bbox = (west, south, east, north)
    levels = task.get("levels") or list(range(task["z_min"], task["z_max"] + 1))
    formats = parse_export(task.get("export", "geotiff"))
    geom = task.get("geometry")
    target_crs = task.get("crs") or ("EPSG:3857" if is_dem else "EPSG:4326")

    # 阶段跟踪器:沿用任务已存的 stages(支持断点续跑 / 单阶段重跑)
    tracker = StageTracker(task_id, task.get("stages") or [], emit,
                           task_name=task.get("name") or task_id)

    update_task(task_id, status="running", message="开始下载")
    emit({"type": "task", "id": task_id, "status": "running"})
    pending = [s["key"] for s in tracker.stages if s["status"] not in ("done", "skipped")]
    logger.info("任务[%s]开始运行 provider=%s 级别=%s 格式=%s 待执行阶段=%s",
                task["name"], task["provider"], levels, formats, pending)

    total = task["total"]
    downloaded = task.get("downloaded", 0)
    failed = task.get("failed", 0)

    def should_stop() -> bool:
        return task_queue.control_of(task_id) in ("pause", "cancel")

    # ---------- 阶段 1:下载原始瓦片 ----------
    if not tracker.is_done("download"):
        import time as _time
        downloaded = 0
        failed = 0
        _last_db = 0.0

        def on_progress(ok_delta: int, fail_delta: int):
            nonlocal downloaded, failed, _last_db
            downloaded += ok_delta
            failed += fail_delta
            # 下载列落库限流 0.5s(避免每张瓦片写库);阶段进度 tracker 内部亦限流
            now = _time.time()
            if now - _last_db >= 0.5:
                _last_db = now
                update_task(task_id, downloaded=downloaded, failed=failed)
                # 同步下载计数到界面头部(卡片/详情的 已下载/总数 读 progress 消息)
                emit({"type": "progress", "id": task_id,
                      "downloaded": downloaded, "failed": failed, "total": total})
            tracker.update("download", done=downloaded, total=total,
                           message=f"{downloaded}/{total}"
                                   + (f",失败{failed}" if failed else ""))

        tracker.start("download", total=total, message="下载原始瓦片")
        logger.info("任务[%s] 阶段[下载] 开始,共 %d 张瓦片", task["name"], total)
        range_fn = mercator_range_for_bbox if is_dem else range_for_bbox
        stopped = False
        for z in levels:
            tr = range_fn(west, south, east, north, z)
            _ok, _fail, stopped = await downloader.download_range(tr, on_progress, should_stop)
            if stopped:
                break
            if anno_downloader is not None:
                _ok, _fail, stopped = await anno_downloader.download_range(tr, on_progress, should_stop)
                if stopped:
                    break

        if stopped:
            return _handle_stop(task_id, tracker, "download", downloaded, failed, total, emit)

        update_task(task_id, downloaded=downloaded, failed=failed)
        tracker.finish("download", message=f"{downloaded}/{total}"
                       + (f",失败{failed}" if failed else ""))
        logger.info("任务[%s] 阶段[下载] 完成:成功 %d,失败 %d", task["name"], downloaded, failed)
        emit({"type": "progress", "id": task_id,
              "downloaded": downloaded, "failed": failed, "total": total})

    # ---------- 阶段 2+:导出(在线程池顺序执行,每阶段独立)----------
    out_dir = Path(task["output_path"]) if task.get("output_path") else settings.output_dir / task_id
    out_dir.mkdir(parents=True, exist_ok=True)

    ctx = _ExportCtx(
        task=task, provider=provider, downloader=downloader,
        anno_downloader=anno_downloader, out_dir=out_dir, bbox=bbox,
        levels=levels, geom=geom, target_crs=target_crs, is_dem=is_dem,
        tracker=tracker, should_stop=should_stop,
    )

    # 阶段 key → 执行器
    if is_dem:
        executors = {"dem": _stage_dem, "tiles": _stage_dem_tiles, "terrain": _stage_terrain}
    else:
        executors = {"geotiff": _stage_geotiff, "tms": _stage_tms, "osm": _stage_osm}

    outputs: list[str] = []
    any_failed = False
    for stage in list(tracker.stages):
        key = stage["key"]
        if key == "download" or key not in executors:
            continue
        if tracker.is_done(key):
            continue
        label = stage.get("label", key)
        # reset 标记(重试/改参数重下):先清空该阶段旧产出,避免残留脏数据;
        # 普通恢复不带此标记,保留已切瓦片做断点续切。
        if stage.get("reset"):
            _clear_stage_output(ctx, key)
            stage.pop("reset", None)
        logger.info("任务[%s] 阶段[%s] 开始", task["name"], label)
        import time as _t
        _t0 = _t.time()
        try:
            produced = await asyncio.to_thread(executors[key], ctx)
            outputs.extend(produced or [])
            tracker.finish(key)
            logger.info("任务[%s] 阶段[%s] 完成,用时 %.1fs,产出 %s",
                        task["name"], label, _t.time() - _t0,
                        [Path(p).name for p in (produced or [])])
        except _Stopped:
            logger.info("任务[%s] 阶段[%s] 被暂停/取消", task["name"], label)
            return _handle_stop(task_id, tracker, key, downloaded, failed, total, emit)
        except Exception as e:  # 单阶段失败不阻断其他独立阶段
            any_failed = True
            tracker.fail(key, message=str(e)[:200])
            logger.exception("任务[%s] 阶段[%s] 失败:%s", task["name"], label, e)

    # ---------- 数据说明文件(汇总当前磁盘上全部已产出成果)----------
    # 单阶段重试时 outputs 仅含本轮产物,补齐其余已完成阶段的成果路径,
    # 避免 metadata 漏列既有成果。
    all_outputs = _collect_outputs(ctx)
    try:
        _write_metadata(ctx, all_outputs, downloaded, failed, total)
    except Exception as e:
        logger.warning("任务[%s] 写 metadata.json 失败:%s", task["name"], e)
    outputs = all_outputs

    # ---------- 汇总任务终态 ----------
    logger.info("任务[%s] 全部阶段结束,%s", task["name"], "有阶段失败" if any_failed else "成功")
    if any_failed:
        msg = f"部分完成:{downloaded} 张成功,{failed} 张失败(有导出阶段失败,可单独重试)"
        update_task(task_id, status="failed", message=msg,
                    output_path=str(out_dir), downloaded=downloaded, failed=failed)
        emit({"type": "task", "id": task_id, "status": "failed",
              "message": msg, "output_path": str(out_dir), "outputs": outputs})
    else:
        msg = f"完成:{downloaded} 张成功,{failed} 张失败"
        update_task(task_id, status="done", message=msg,
                    output_path=str(out_dir), downloaded=downloaded, failed=failed)
        emit({"type": "task", "id": task_id, "status": "done",
              "message": msg, "output_path": str(out_dir), "outputs": outputs})


def _handle_stop(task_id, tracker, key, downloaded, failed, total, emit):
    """暂停/取消:把当前阶段标记为对应状态,落库任务状态后返回。"""
    ctrl = task_queue.control_of(task_id)
    if ctrl == "cancel":
        tracker.pause(key, message="已取消")
        update_task(task_id, downloaded=downloaded, failed=failed,
                    status="canceled", message="已取消")
        emit({"type": "task", "id": task_id, "status": "canceled", "message": "已取消"})
    else:
        tracker.pause(key, message="已暂停")
        m = f"已暂停({downloaded}/{total})"
        update_task(task_id, downloaded=downloaded, failed=failed,
                    status="paused", message=m)
        emit({"type": "task", "id": task_id, "status": "paused", "message": m})


class _ExportCtx:
    """导出阶段共享上下文(把一堆参数打包,便于各执行器取用)。"""
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _check_stop(ctx):
    """导出阶段内的停止钩子:被暂停/取消时抛 _Stopped 跳回顶层。"""
    if ctx.should_stop():
        raise _Stopped()


# ============ 影像管线阶段 ============

def _stage_geotiff(ctx) -> list[str]:
    """每个选中级别各合并导出一张 {任务名}_z{级别}.tif。

    进度按"瓦片行"细粒度上报(所有层级的行数之和作分母),避免高层级卡死进度条。
    """
    task = ctx.task
    anno_path_fn = ctx.anno_downloader.tile_path if ctx.anno_downloader is not None else None
    levels = ctx.levels
    trs = {z: range_for_bbox(*ctx.bbox, z) for z in levels}
    total_rows = sum(trs[z].rows for z in levels)
    ctx.tracker.start("geotiff", total=total_rows, message="合并 GeoTIFF")
    outputs = []
    base_done = 0
    for z in levels:
        _check_stop(ctx)
        tr = trs[z]

        def on_row(done_rows, total_r, _z=z, _base=base_done):
            _check_stop(ctx)
            ctx.tracker.update("geotiff", done=_base + done_rows,
                               message=f"拼接第 {_z} 级({done_rows}/{total_r} 行)")

        # 主文件恒为 EPSG:4326(供 OSM/TMS 切片复用,不必再自拼源)。
        # 裁剪在 4326 下做(几何本身即 WGS84,直接匹配)。
        geotiff = ctx.out_dir / f"{task['name']}_z{z}.tif"
        mosaic_to_geotiff(ctx.provider, settings.cache_dir, tr, geotiff,
                          ctx.downloader.tile_path, anno_path_fn, on_row=on_row)

        # 裁剪 + 需要 OSM 时:裁剪前把最高级那张未裁剪 4326 图留一份给 OSM 复用。
        # OSM 需未裁剪源(自带几何遮罩精确切边),裁过的源会在几何边缘产生暗边。
        # 这样 OSM 阶段直接捡起此源,彻底不必自拼(裁剪场景也免重拼)。
        _need_osm = "osm" in parse_export(task.get("export", "geotiff"))
        _clipping = bool(task.get("clip") and ctx.geom)
        if z == max(levels) and _need_osm and _clipping:
            import shutil as _shutil
            osm_src = ctx.out_dir / f".osm_src_z{z}.tif"
            _shutil.copyfile(geotiff, osm_src)

        if _clipping:
            clip_to_geometry(geotiff, ctx.geom)
        outputs.append(str(geotiff))

        # 目标坐标系非 4326:另存一份重投影版 {name}_z{z}_{epsg}.tif,
        # 主 4326 文件保留。裁剪已在 4326 版做过,重投影版继承裁剪结果。
        if ctx.target_crs and ctx.target_crs != "EPSG:4326":
            epsg = ctx.target_crs.split(":")[-1]
            proj_tif = ctx.out_dir / f"{task['name']}_z{z}_{epsg}.tif"
            import shutil as _shutil
            _shutil.copyfile(geotiff, proj_tif)
            reproject_geotiff(proj_tif, ctx.target_crs)
            outputs.append(str(proj_tif))

        base_done += tr.rows
        ctx.tracker.update("geotiff", done=base_done, message=f"第 {z} 级完成")
    return outputs


def _stage_tms(ctx) -> list[str]:
    """输出 gdal2tiles geodetic TMS 瓦片包 + tilemapresource.xml。

    进度按瓦片数细粒度上报。
    """
    task = ctx.task
    anno_path_fn = ctx.anno_downloader.tile_path if ctx.anno_downloader is not None else None
    clip_geom = ctx.geom if (task.get("clip") and ctx.geom) else None
    levels = ctx.levels
    ctx.tracker.start("tms", total=1, message="切 TMS 瓦片")
    tms_dir = ctx.out_dir / "tms"

    def on_progress(done, total):
        ctx.tracker.update("tms", done=done, total=total,
                           message=f"已切 {done}/{total} 张")

    _, _, tms_ext, stopped = export_tms(
        ctx.provider, ctx.downloader.tile_path, ctx.bbox, levels, tms_dir,
        clip_geom=clip_geom, anno_tile_path_fn=anno_path_fn,
        on_progress=on_progress, should_stop=ctx.should_stop)
    if stopped:
        raise _Stopped()
    write_tilemapresource(tms_dir, ctx.provider, task["name"], ctx.bbox, levels, ext=tms_ext)
    return [str(tms_dir)]


def _stage_osm(ctx) -> list[str]:
    """重投影到 Web 墨卡托后切 XYZ 瓦片。源用最高级 4326 拼接图。

    进度分两段:拼源(按行)约占前 20%,切片(按瓦片)占后 80%,统一映射到 0-1000
    的内部刻度上报,保证平滑推进。
    """
    task = ctx.task
    anno_path_fn = ctx.anno_downloader.tile_path if ctx.anno_downloader is not None else None
    clip_geom = ctx.geom if (task.get("clip") and ctx.geom) else None
    z_max = max(ctx.levels)
    osm_levels = list(range(min(OSM_MIN_LEVEL, z_max), z_max + 1))
    # 进度直接用真实瓦片数上报(切片阶段),不再用 0-1000 虚拟刻度——虚拟刻度让
    # tracker 拿到的增量极小且不均匀,速率/ETA 计算会失真(速率显示 0、剩余时间乱跳)。
    ctx.tracker.start("osm", total=1, message="准备 OSM 源")

    tr_max = range_for_bbox(*ctx.bbox, z_max)
    osm_src = ctx.out_dir / f".osm_src_z{z_max}.tif"

    # OSM 源必须是 EPSG:4326 未裁剪 RGB 图。geotiff 阶段的主文件 {name}_z{z}.tif
    # 恒为 4326 版(目标投影另存 _{epsg}.tif),故只要 geotiff 阶段完成即可复用,
    # 不再受输出坐标系限制,省掉一次最高级重复拼接。
    # 裁剪时不复用裁过的主文件(边界外 nodata 会与 OSM 自带遮罩重复处理、产生暗边),
    # 但 geotiff 阶段已在裁剪前把最高级未裁剪源留到 .osm_src_z{z}.tif,
    # 下面的 elif 分支会捡起它——裁剪场景同样免自拼。
    formats = parse_export(task.get("export", "geotiff"))
    clipped = bool(task.get("clip") and ctx.geom)
    geotiff_src = ctx.out_dir / f"{task['name']}_z{z_max}.tif"
    can_reuse_geotiff = (
        "geotiff" in formats
        and not clipped
        and ctx.tracker.is_done("geotiff")
        and geotiff_src.exists() and geotiff_src.stat().st_size > 0
    )

    reused = False
    if can_reuse_geotiff:
        src_path = geotiff_src
        reused = True
        ctx.tracker.update("osm", message="复用合并的 GeoTIFF 作为 OSM 源")
        logger.info("任务[%s] 阶段[OSM] 复用合并 GeoTIFF(%s)作源,跳过重拼",
                    task["name"], geotiff_src.name)
    elif osm_src.exists() and osm_src.stat().st_size > 0:
        # 断点续切:上次拼好并保留的自拼源,跳过重拼直接切片。
        src_path = osm_src
        ctx.tracker.update("osm", message="复用已拼 OSM 源,继续切片")
        logger.info("任务[%s] 阶段[OSM] 复用已拼源图,跳过重拼", task["name"])
    else:
        def on_row(done_rows, total_r):
            _check_stop(ctx)
            ctx.tracker.update("osm", message=f"拼接 OSM 源({done_rows}/{total_r} 行)")

        # 拼源用临时文件 + 原子改名,中途暂停留下的是 .tmp(不会被误当完整源)。
        tmp_src = ctx.out_dir / f".osm_src_z{z_max}.tmp.tif"
        mosaic_to_geotiff(ctx.provider, settings.cache_dir, tr_max, tmp_src,
                          ctx.downloader.tile_path, anno_path_fn, on_row=on_row)
        tmp_src.replace(osm_src)   # 完整拼好才改名为正式源
        src_path = osm_src
        ctx.tracker.update("osm", message="切 OSM 瓦片")

    # 切片阶段进度:直接上报真实瓦片数(export_osm 给的 done/total 即真实瓦片计数),
    # 速率就是真实"张/秒",ETA 基于真实量、平滑有意义。切片开始时把 total 校正为真实值。
    def on_progress(done, total):
        _check_stop(ctx)
        ctx.tracker.update("osm", done=done, total=total,
                           message=f"切 OSM 瓦片({done}/{total} 张)")

    osm_dir = ctx.out_dir / "osm"
    _, _, stopped = export_osm(src_path, osm_levels, ctx.bbox, osm_dir,
                               on_progress=on_progress, clip_geom=clip_geom,
                               should_stop=ctx.should_stop)
    if stopped:
        raise _Stopped()          # 保留源,供恢复时复用、不重拼
    # 只删自拼的临时源;复用的 geotiff 是正式成果,保留。
    # 用安全删除:删不掉不影响已切好的成果(阶段仍算成功)。
    if not reused:
        _safe_unlink(osm_src)
    return [str(osm_dir)]


# ============ DEM 管线阶段 ============

def _hs_params(task):
    hs = task.get("hillshade") or {}
    return (hs.get("azimuth", 315.0), hs.get("altitude", 45.0), hs.get("z_factor", 1.0))


def _stage_dem(ctx) -> list[str]:
    """高程 GeoTIFF(每级一张)+ 可选晕渲图。"""
    task = ctx.task
    formats = parse_export(task.get("export", "geotiff"))
    want_geotiff = "geotiff" in formats
    want_hillshade = "hillshade" in formats
    az, alt, zf = _hs_params(task)
    levels = ctx.levels
    trs = {z: mercator_range_for_bbox(*ctx.bbox, z) for z in levels}
    total_rows = sum(trs[z].rows for z in levels)
    ctx.tracker.start("dem", total=total_rows, message="解码拼接高程")
    outputs = []
    base_done = 0
    for z in levels:
        _check_stop(ctx)
        tr = trs[z]

        def on_row(done_rows, total_r, _z=z, _base=base_done):
            _check_stop(ctx)
            ctx.tracker.update("dem", done=_base + done_rows,
                               message=f"解码高程第 {_z} 级({done_rows}/{total_r} 行)")

        dem_tif = ctx.out_dir / f"{task['name']}_dem_z{z}.tif"
        mosaic_dem_geotiff(tr, dem_tif, ctx.downloader.tile_path, on_row=on_row)
        if want_hillshade:
            hs_tif = ctx.out_dir / f"{task['name']}_hillshade_z{z}.tif"
            hillshade_from_dem(dem_tif, hs_tif, azimuth=az, altitude=alt, z_factor=zf)
            if ctx.target_crs and ctx.target_crs not in ("EPSG:3857",):
                reproject_geotiff(hs_tif, ctx.target_crs)
            outputs.append(str(hs_tif))
        if want_geotiff:
            if ctx.target_crs and ctx.target_crs not in ("EPSG:3857",):
                reproject_geotiff(dem_tif, ctx.target_crs)
            outputs.append(str(dem_tif))
        else:
            _safe_unlink(dem_tif)
        base_done += tr.rows
        ctx.tracker.update("dem", done=base_done, message=f"第 {z} 级完成")
    return outputs


def _stage_dem_tiles(ctx) -> list[str]:
    """保留原始 LERC 瓦片包:{tiles}/{z}/{x}/{y}.lerc。"""
    import shutil as _shutil
    ext = ctx.provider.ext
    tiles_dir = ctx.out_dir / "tiles"
    levels = ctx.levels
    ctx.tracker.start("tiles", total=len(levels), message="导出原始瓦片")
    for i, z in enumerate(levels, start=1):
        _check_stop(ctx)
        ctx.tracker.update("tiles", done=i - 1, message=f"第 {z} 级")
        tr = mercator_range_for_bbox(*ctx.bbox, z)
        for x, y in tr.iter_tiles():
            src = ctx.downloader.tile_path(x, y, z)
            if not (src.exists() and src.stat().st_size > 0):
                continue
            dst = tiles_dir / str(z) / str(x) / f"{y}.{ext}"
            dst.parent.mkdir(parents=True, exist_ok=True)
            _shutil.copyfile(src, dst)
        ctx.tracker.update("tiles", done=i, message=f"第 {z} 级完成")
    return [str(tiles_dir)]


def _stage_terrain(ctx) -> list[str]:
    """Cesium quantized-mesh 地形切片(terrain/ + layer.json)。

    切片阶段进度直接上报真实瓦片数(不用虚拟刻度,速率/ETA 才准);
    准备高程源那段只更新消息文字、不占数字刻度。
    """
    import rasterio as _rio
    from rasterio.warp import calculate_default_transform as _cdt
    max_z = max(ctx.levels)
    tr = mercator_range_for_bbox(*ctx.bbox, max_z)
    # 已重投影到 4326 的高程源(准备好才改名为此正式名,供恢复复用)
    terrain_src = ctx.out_dir / "_terrain_src_4326.tif"
    ctx.tracker.start("terrain", total=1, message="准备地形高程源")

    # 断点续切:4326 高程源已备好则跳过拼接+重投影,直接切片
    if terrain_src.exists() and terrain_src.stat().st_size > 0:
        ctx.tracker.update("terrain", message="复用已备高程源,继续切片")
        logger.info("任务[%s] 阶段[terrain] 复用已备高程源,跳过重拼", ctx.task["name"])
    else:
        def on_row(done_rows, total_r):
            _check_stop(ctx)
            ctx.tracker.update("terrain", message=f"准备高程源({done_rows}/{total_r} 行)")

        tmp_src = ctx.out_dir / "_terrain_src.tmp.tif"
        mosaic_dem_geotiff(tr, tmp_src, ctx.downloader.tile_path,
                           build_overviews=False, on_row=on_row)
        reproject_geotiff(tmp_src, "EPSG:4326")
        _check_stop(ctx)
        tmp_src.replace(terrain_src)   # 拼接+重投影都完成才改名为正式源
        ctx.tracker.update("terrain", message="切 Cesium 地形")

    with _rio.open(terrain_src) as _ds:
        _t, _w, _h = _cdt(_ds.crs, "EPSG:4326", _ds.width, _ds.height, *_ds.bounds)
        deg_per_px = abs(_t.a)
    Lmax = level_for_resolution(deg_per_px)
    t_levels = list(range(0, Lmax + 1))

    terrain_dir = ctx.out_dir / "terrain"

    # 切片进度:直接上报真实瓦片数(export_terrain 给的 done/total 即真实计数)
    def on_progress(done, total):
        _check_stop(ctx)
        ctx.tracker.update("terrain", done=done, total=total,
                           message=f"切 Cesium 地形({done}/{total} 张)")

    _, t_exported, stopped = export_terrain(terrain_src, t_levels, ctx.bbox,
                                            terrain_dir, on_progress=on_progress,
                                            should_stop=ctx.should_stop)
    if stopped:
        raise _Stopped()          # 保留高程源,供恢复复用、不重拼
    write_layer_json(terrain_dir, ctx.bbox, t_exported)
    _safe_unlink(terrain_src)  # 全部切完才删源(删不掉不影响成果)
    return [str(terrain_dir)]


def _clear_stage_output(ctx, key: str) -> None:
    """清空某导出阶段的旧产出(重试/改参数重下时调用),避免残留脏数据。

    普通恢复不会调用此函数——那时保留已切瓦片以断点续切。
    """
    import shutil as _shutil
    task = ctx.task
    out_dir = ctx.out_dir

    def rm(p: Path):
        try:
            if p.is_dir():
                _shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("清理阶段产出失败 %s:%s", p, e)

    if key == "tms":
        rm(out_dir / "tms")
    elif key == "osm":
        rm(out_dir / "osm")
        # 连同保留的中间源图一并清掉,避免重来时复用旧源
        for p in out_dir.glob(".osm_src_*.tif"):
            rm(p)
    elif key == "terrain":
        rm(out_dir / "terrain")
        rm(out_dir / "_terrain_src_4326.tif")
        rm(out_dir / "_terrain_src.tmp.tif")
    elif key == "tiles":
        rm(out_dir / "tiles")
    elif key == "geotiff":
        epsg = (ctx.target_crs or "").split(":")[-1]
        for z in ctx.levels:
            rm(out_dir / f"{task['name']}_z{z}.tif")
            if epsg and ctx.target_crs != "EPSG:4326":
                rm(out_dir / f"{task['name']}_z{z}_{epsg}.tif")
    elif key == "dem":
        for z in ctx.levels:
            rm(out_dir / f"{task['name']}_dem_z{z}.tif")
            rm(out_dir / f"{task['name']}_hillshade_z{z}.tif")
    logger.info("任务[%s] 阶段[%s] 旧产出已清空(重来)", task["name"], key)


def _collect_outputs(ctx) -> list[str]:
    """按已完成阶段扫描磁盘,汇总当前全部成果路径(供 metadata 汇总)。

    单阶段重试时本轮 outputs 只含该阶段产物,这里据已完成阶段与固定命名
    重建完整清单,避免漏列既有成果。仅收录实际存在的文件/目录。
    """
    task = ctx.task
    out_dir = ctx.out_dir
    tr = ctx.tracker
    outs: list[str] = []

    def add(p: Path):
        if p.exists():
            outs.append(str(p))

    if ctx.is_dem:
        formats = parse_export(task.get("export", "geotiff"))
        if tr.is_done("dem"):
            for z in ctx.levels:
                if "hillshade" in formats:
                    add(out_dir / f"{task['name']}_hillshade_z{z}.tif")
                if "geotiff" in formats:
                    add(out_dir / f"{task['name']}_dem_z{z}.tif")
        if tr.is_done("tiles"):
            add(out_dir / "tiles")
        if tr.is_done("terrain"):
            add(out_dir / "terrain")
    else:
        if tr.is_done("geotiff"):
            epsg = (ctx.target_crs or "").split(":")[-1]
            for z in ctx.levels:
                add(out_dir / f"{task['name']}_z{z}.tif")
                if epsg and ctx.target_crs != "EPSG:4326":
                    add(out_dir / f"{task['name']}_z{z}_{epsg}.tif")
        if tr.is_done("tms"):
            add(out_dir / "tms")
        if tr.is_done("osm"):
            add(out_dir / "osm")
    return outs


def _write_metadata(ctx, outputs, downloaded, failed, total):
    """写 metadata.json,汇总当前范围/级别/格式与已产出成果。"""
    task = ctx.task
    formats = parse_export(task.get("export", "geotiff"))
    if ctx.is_dem:
        az, alt, zf = _hs_params(task)
        hillshade = ({"azimuth": az, "altitude": alt, "z_factor": zf}
                     if "hillshade" in formats else None)
        write_metadata(
            ctx.out_dir, task_id=task["id"], name=task["name"], provider=ctx.provider,
            provider_key=task["provider"], bbox=ctx.bbox, levels=ctx.levels,
            crs=(ctx.target_crs or "EPSG:3857"), export_formats=formats or ["geotiff"],
            downloaded=downloaded, failed=failed, total=total, outputs=outputs,
            clip=False, annotate=False, dem=True, hillshade=hillshade,
        )
    else:
        write_metadata(
            ctx.out_dir, task_id=task["id"], name=task["name"], provider=ctx.provider,
            provider_key=task["provider"], bbox=ctx.bbox, levels=ctx.levels,
            crs=ctx.target_crs, export_formats=formats,
            downloaded=downloaded, failed=failed, total=total, outputs=outputs,
            clip=bool(task.get("clip") and ctx.geom),
            annotate=ctx.anno_downloader is not None,
        )
