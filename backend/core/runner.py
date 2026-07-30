"""任务执行:阶段化管线(下载 → 合并 → 各切片格式),逐阶段上报进度。

被 TaskQueue 调用:async run_task(task_id, emit)。
emit(msg: dict) 把进度同步广播给 WebSocket 订阅者。

阶段模型(见 models.build_stage_defs 与 core.formats 注册表):
  影像:download → geotiff → tms → osm
  DEM :download → dem(高程/晕渲) → tms → osm → tiles → terrain → contour
各阶段相互独立:已完成(done)的阶段跳过(断点续跑 / 单阶段重跑),
单个导出阶段失败只标记该阶段 failed,不阻断其余阶段。任务终态由阶段汇总。

"哪种数据能出哪些格式"由 core.formats 声明,本模块负责给出对应实现:
_executors_for 按数据类型组装执行器表,两者的一致性在模块导入时校验
(见文件末尾 _assert_executors_cover),漏实现会立刻报错而不是静默空转。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from ..config import settings
from ..models import get_task, parse_export, update_task
from ..providers.buildings import is_building_provider
from ..providers.terrain import build_terrain_provider, is_dem_provider
from ..providers.tianditu import build_annotation_provider, build_provider
from .containers import container_of, convert_raster
from .contour import DEFAULT_INTERVAL, extract_contours, write_contours
from .mbtiles import pack_mbtiles
from .dem import hillshade_from_dem, mosaic_dem_geotiff
from .dem_tiling import mercator_range_for_bbox
from .downloader import TileDownloader
from .formats import CONTAINERS, DataKind, STAGES, kind_of, resolve_outputs
from .logs import logger
from .metadata import write_metadata
from .mosaic import mosaic_to_geotiff
from .osm import export_osm
from .postprocess import clip_to_geometry, reproject_geotiff
from .progress import StageTracker
from .queue import task_queue
from .terrain_tiles import export_terrain, level_for_resolution, write_layer_json
from .token_pool import token_pool
from .tms import export_tms, export_tms_from_source, write_tilemapresource
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
        tracker=tracker, should_stop=should_stop, cur_stage="",
        # 延后到全部阶段结束后再做的容器转换(见 _stage_geotiff 的说明)
        deferred_convert=[],
    )

    # 阶段 key → 执行器。按数据类型组装(取代原先的 if is_dem 二选一):
    # tms/osm 两种栅格都支持,DEM 侧靠 _dem_visual_source 先渲染成 RGB 再复用
    # 同一套切片器,避免为 DEM 重写一遍切片逻辑。
    executors = _executors_for(kind_of(task["provider"]))

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
        ctx.cur_stage = key      # 供跨阶段共享的辅助函数(如 _dem_visual_source)上报进度
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

    # ---------- 延后的容器转换 ----------
    # geotiff 主文件被 tms/osm 当切片源,故转换排到所有阶段跑完才做(见 _stage_geotiff)。
    for stage_key, container in list(ctx.deferred_convert):
        try:
            _convert_deferred(ctx, stage_key, container)
        except Exception as e:
            logger.warning("任务[%s] 延后的容器转换失败(%s→%s):%s,保留 GeoTIFF",
                           task["name"], stage_key, container, e)
    ctx.deferred_convert.clear()

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


def _executors_for(kind: str) -> dict:
    """按数据类型组装"阶段 key → 执行器"。

    注册表(core.formats)声明了某数据类型能出哪些格式,这里必须给出对应实现,
    否则用户勾了会得到一个空转的阶段。两者的一致性由 _assert_executors_cover
    在导入时校验。
    """
    if kind == DataKind.RASTER_DEM:
        return {
            "dem": _stage_dem,
            "tiles": _stage_dem_tiles,
            "terrain": _stage_terrain,
            # DEM 复用影像的切片器:先把高程渲染成 RGB 4326 图当源
            "tms": _stage_tms,
            "osm": _stage_osm,
            "contour": _stage_contour,
        }
    return {"geotiff": _stage_geotiff, "tms": _stage_tms, "osm": _stage_osm}


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

    # 容器转换放在最后、且只在没有下游阶段要复用主文件时立即执行。
    # 原因:tms/osm 阶段会把 {name}_z{z}.tif 当切片源(见 _stage_osm 的复用逻辑),
    # 若这里先转成 PNG,下游就拿不到可读的栅格源了。COG 仍是合法 GeoTIFF,不受影响。
    container = container_of(task, "geotiff")
    if container not in ("", "gtiff"):
        pending = _downstream_needs_geotiff(ctx)
        if pending:
            ctx.deferred_convert.append(("geotiff", container))
            logger.info("任务[%s] geotiff 容器转换延后到 %s 阶段之后",
                        task["name"], "/".join(pending))
        else:
            outputs = _convert_stage_outputs(outputs, container)
    return outputs


def _downstream_needs_geotiff(ctx) -> list[str]:
    """列出还未完成、且会复用 geotiff 主文件当源的阶段。"""
    formats = parse_export(ctx.task.get("export", "geotiff"))
    return [k for k in ("tms", "osm")
            if k in formats and not ctx.tracker.is_done(k)]


def _convert_deferred(ctx, stage_key: str, container: str) -> None:
    """执行延后的容器转换:按注册表模板找出该阶段的产出文件逐个转换。"""
    task = ctx.task
    epsg = (ctx.target_crs or "").split(":")[-1]
    targets: list[Path] = []
    for rel in resolve_outputs(stage_key, "gtiff", task["name"], ctx.levels):
        targets.append(ctx.out_dir / rel)
    if stage_key == "geotiff" and epsg and ctx.target_crs != "EPSG:4326":
        targets += [ctx.out_dir / f"{task['name']}_z{z}_{epsg}.tif"
                    for z in ctx.levels]
    for p in targets:
        if p.exists():
            convert_raster(p, container)


def _convert_stage_outputs(outputs: list[str], container: str) -> list[str]:
    """把一批 GeoTIFF 产出转成目标容器,返回替换后的路径列表。"""
    converted: list[str] = []
    for p in outputs:
        src = Path(p)
        # 重投影副本(带 _{epsg} 后缀)一并转换,保持两份成果格式一致
        new = convert_raster(src, container)
        converted.append(str(new) if new else p)
    return converted


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

    # DEM 走另一条路:export_tms 是"从缓存逐张搬运瓦片",而 DEM 缓存里是 3857 网格的
    # LERC 编码瓦片,既非 4326 行列号也不是图片,搬不过来。故先渲染成 4326 RGB 源,
    # 再按 TMS 网格重新切分(_export_tms_from_source)。
    if ctx.is_dem:
        return _tms_from_dem(ctx, tms_dir, clip_geom)

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
    return _maybe_mbtiles(ctx, "tms", tms_dir, "tms", tms_ext)


def _maybe_mbtiles(ctx, stage_key: str, tiles_dir: Path, scheme: str,
                   tile_ext: str) -> list[str]:
    """选了 MBTiles 容器时把瓦片目录打包成单文件,否则原样返回目录。

    打包成功后删除瓦片目录:容器是"换一种装法",不是"多一份成果";
    留着目录会让用户以为要两个都拷走,而 mbtiles 已含全部瓦片。
    """
    if container_of(ctx.task, stage_key) != "mbtiles":
        return [str(tiles_dir)]

    def on_progress(done, total):
        ctx.tracker.update(stage_key, message=f"打包 MBTiles({done}/{total} 张)")

    out = ctx.out_dir / f"{ctx.task['name']}_{stage_key}.mbtiles"
    packed = pack_mbtiles(tiles_dir, out, scheme=scheme, name=ctx.task["name"],
                          bbox=ctx.bbox, tile_format=tile_ext,
                          should_stop=ctx.should_stop, on_progress=on_progress)
    if packed is None:
        return [str(tiles_dir)]
    import shutil as _shutil
    _shutil.rmtree(tiles_dir, ignore_errors=True)
    return [str(packed)]


def _tms_from_dem(ctx, tms_dir: Path, clip_geom) -> list[str]:
    """DEM 出 TMS:先渲染 4326 可视化源,再重采样切 geodetic 网格。"""
    src = _dem_visual_source(ctx)

    def on_progress(done, total):
        _check_stop(ctx)
        ctx.tracker.update("tms", done=done, total=total,
                           message=f"切 TMS 瓦片({done}/{total} 张)")

    _, _, tms_ext, stopped = export_tms_from_source(
        src, ctx.bbox, ctx.levels, tms_dir, clip_geom=clip_geom,
        on_progress=on_progress, should_stop=ctx.should_stop)
    if stopped:
        raise _Stopped()          # 保留可视化源,恢复时复用
    write_tilemapresource(tms_dir, ctx.provider, ctx.task["name"],
                          ctx.bbox, ctx.levels, ext=tms_ext)
    _safe_unlink(src)
    return _maybe_mbtiles(ctx, "tms", tms_dir, "tms", tms_ext)


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

    # DEM 的源不是影像 geotiff,而是渲染出的 4326 可视化图。切完由本分支自己清理:
    # 它可能同时被 tms 阶段用到,故不走下面 reused 的删除逻辑。
    if ctx.is_dem:
        src_path = _dem_visual_source(ctx)
        ctx.tracker.update("osm", message="切 OSM 瓦片")

        def on_progress_dem(done, total):
            _check_stop(ctx)
            ctx.tracker.update("osm", done=done, total=total,
                               message=f"切 OSM 瓦片({done}/{total} 张)")

        osm_dir = ctx.out_dir / "osm"
        _, _, stopped = export_osm(src_path, osm_levels, ctx.bbox, osm_dir,
                                   on_progress=on_progress_dem,
                                   clip_geom=clip_geom,
                                   should_stop=ctx.should_stop)
        if stopped:
            raise _Stopped()      # 保留可视化源,恢复时复用
        _safe_unlink(src_path)
        return _maybe_mbtiles(ctx, "osm", osm_dir, "xyz", "png")

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
    return _maybe_mbtiles(ctx, "osm", osm_dir, "xyz", "png")


# ============ DEM → 可视化 RGB 源(供复用影像切片器)============

def _dem_visual_source(ctx) -> Path:
    """把 DEM 渲染成 EPSG:4326 的 RGB 图,供 tms/osm 切片器复用。

    为什么需要这一步:切片器是给影像写的,吃 RGB;而 DEM 是单波段浮点高程,
    直接切出来的瓦片没法看(取值动辄几千,超出 8bit)。这里用与"晕渲图"一致的
    参数渲染成灰度 RGB,既能看地形起伏,也复用了已有的 hillshade 实现。

    另一处必须处理的差异:DEM 瓦片网格是 EPSG:3857 墨卡托,而切片器要求 4326
    源(_stage_terrain 也是同样先重投影),故渲染后再投一次。

    产物 .dem_vis_z{z}.tif 保留到阶段成功为止,支持断点续切时免重拼。
    """
    task = ctx.task
    z_max = max(ctx.levels)
    vis = ctx.out_dir / f".dem_vis_z{z_max}.tif"
    if vis.exists() and vis.stat().st_size > 0:
        logger.info("任务[%s] 复用已备 DEM 可视化源,跳过重拼", task["name"])
        return vis

    az, alt, zf = _hs_params(task)
    tr = mercator_range_for_bbox(*ctx.bbox, z_max)

    def on_row(done_rows, total_r):
        _check_stop(ctx)
        ctx.tracker.update(ctx.cur_stage,
                           message=f"准备高程可视化源({done_rows}/{total_r} 行)")

    tmp_dem = ctx.out_dir / f".dem_vis_src_z{z_max}.tmp.tif"
    tmp_gray = ctx.out_dir / f".dem_vis_gray_z{z_max}.tmp.tif"
    tmp_vis = ctx.out_dir / f".dem_vis_z{z_max}.tmp.tif"
    mosaic_dem_geotiff(tr, tmp_dem, ctx.downloader.tile_path,
                       build_overviews=False, on_row=on_row)
    _check_stop(ctx)
    # 渲染成灰度晕渲(单波段 8bit)
    hillshade_from_dem(tmp_dem, tmp_gray, azimuth=az, altitude=alt,
                       z_factor=zf, build_overviews=False)
    _safe_unlink(tmp_dem)
    _check_stop(ctx)
    # 灰度扩成三波段 RGB:export_osm 按源波段数出瓦片,单波段源会出成"灰度+alpha"
    # 的 2 波段 PNG(能看但与影像成果的 RGBA 不一致,部分 GIS 客户端也不认)。
    _gray_to_rgb(tmp_gray, tmp_vis)
    _safe_unlink(tmp_gray)
    _check_stop(ctx)
    reproject_geotiff(tmp_vis, "EPSG:4326")
    tmp_vis.replace(vis)      # 全部就绪才改名为正式源
    return vis


def _gray_to_rgb(src_path: Path, dst_path: Path) -> Path:
    """把单波段灰度图复制成三波段 RGB(保持坐标与网格不变)。"""
    import rasterio as _rio
    from rasterio.enums import ColorInterp as _CI
    with _rio.open(src_path) as src:
        band = src.read(1)
        profile = src.profile.copy()
        mask = src.read_masks(1)
    profile.update(count=3, dtype="uint8", photometric="RGB")
    profile.pop("nodata", None)
    with _rio.open(dst_path, "w", **profile) as dst:
        for i in range(1, 4):
            dst.write(band, i)
        dst.colorinterp = [_CI.red, _CI.green, _CI.blue]
        # 无数据区靠 mask 传下去,切片时才有正确 alpha
        dst.write_mask(mask)
    return dst_path


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

    # 容器转换。与影像同理:contour/tms/osm 阶段会复用高程图当源,故有下游时延后。
    container = container_of(task, "dem")
    if container not in ("", "gtiff"):
        pending = [k for k in ("tms", "osm", "contour")
                   if k in formats and not ctx.tracker.is_done(k)]
        if pending:
            ctx.deferred_convert.append(("dem", container))
            logger.info("任务[%s] dem 容器转换延后到 %s 阶段之后",
                        task["name"], "/".join(pending))
        else:
            outputs = _convert_stage_outputs(outputs, container)
    return outputs


def _stage_contour(ctx) -> list[str]:
    """从最高级高程图提取等高线,写成矢量成果。

    源的取法:优先复用 dem 阶段已产出的 {name}_dem_z{z}.tif(免重拼);未勾选高程
    GeoTIFF 时自行拼一张临时高程图。注意不能用 target_crs 重投影后的版本——
    等高距是按高程值算的,与平面坐标系无关,但重投影会重采样高程、略微改变取值。
    """
    task = ctx.task
    z_max = max(ctx.levels)
    interval = float(task.get("contour_interval") or DEFAULT_INTERVAL)
    container = container_of(task, "contour") or "geojson"
    cont = CONTAINERS.get(container) or CONTAINERS["geojson"]

    ctx.tracker.start("contour", total=1, message="准备高程源")
    dem_tif = ctx.out_dir / f"{task['name']}_dem_z{z_max}.tif"
    tmp_dem = None
    if not (dem_tif.exists() and dem_tif.stat().st_size > 0):
        def on_row(done_rows, total_r):
            _check_stop(ctx)
            ctx.tracker.update("contour",
                               message=f"准备高程源({done_rows}/{total_r} 行)")

        tmp_dem = ctx.out_dir / f".contour_src_z{z_max}.tif"
        tr = mercator_range_for_bbox(*ctx.bbox, z_max)
        mosaic_dem_geotiff(tr, tmp_dem, ctx.downloader.tile_path,
                           build_overviews=False, on_row=on_row)
        # 等高线成果统一 WGS84(与其余矢量成果一致,前端可直读)
        reproject_geotiff(tmp_dem, "EPSG:4326")
        src = tmp_dem
    else:
        src = dem_tif
        ctx.tracker.update("contour", message="复用已合并高程图")

    def on_progress(done, total):
        _check_stop(ctx)
        ctx.tracker.update("contour", done=done, total=total,
                           message=f"提取等高线({done}/{total} 条等高距)")

    geoms, values, crs = extract_contours(src, interval=interval,
                                          should_stop=ctx.should_stop,
                                          on_progress=on_progress)
    if ctx.should_stop():
        if tmp_dem is not None:
            _safe_unlink(tmp_dem)
        raise _Stopped()

    out_path = ctx.out_dir / f"{task['name']}_contour{cont.ext}"
    write_contours(out_path, geoms, values, crs, container)
    if tmp_dem is not None:
        _safe_unlink(tmp_dem)
    ctx.tracker.update("contour", message=f"共 {len(geoms)} 条等高线")
    return [str(out_path)]


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

    # 正式产出物路径由注册表推导(阶段声明了 outputs 模板),不再逐阶段手写。
    container = (task.get("containers") or {}).get(key)
    if not container:
        stage = STAGES.get(key)
        container = stage.containers[0] if (stage and stage.containers) else ""
    sidecars = CONTAINERS[container].sidecars if container in CONTAINERS else ()
    for rel in resolve_outputs(key, container, task["name"], ctx.levels):
        target = out_dir / rel.rstrip("/")
        rm(target)
        # shapefile 等多文件格式的附属文件(.shx/.dbf/.prj/.cpg)必须一并清掉,
        # 否则残留的 .dbf 会与新写的 .shp 记录数不符,成果直接打不开。
        for suffix in sidecars:
            rm(target.with_suffix(suffix))

    # 中间文件与坐标系变体仍需按阶段处理:它们不是"成果",不在注册表的 outputs 里。
    if key == "osm":
        # 保留的中间源图一并清掉,避免重来时复用旧源
        for p in out_dir.glob(".osm_src_*.tif"):
            rm(p)
        for p in out_dir.glob(".dem_vis_*.tif"):
            rm(p)
    elif key == "tms":
        for p in out_dir.glob(".dem_vis_*.tif"):
            rm(p)
    elif key == "terrain":
        rm(out_dir / "_terrain_src_4326.tif")
        rm(out_dir / "_terrain_src.tmp.tif")
    elif key == "contour":
        for p in out_dir.glob(".contour_src_*.tif"):
            rm(p)
    elif key == "geotiff":
        # 重投影后的副本文件名带 EPSG 号,不在 outputs 模板里
        epsg = (ctx.target_crs or "").split(":")[-1]
        if epsg and ctx.target_crs != "EPSG:4326":
            for z in ctx.levels:
                rm(out_dir / f"{task['name']}_z{z}_{epsg}.tif")
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

    formats = parse_export(task.get("export", "geotiff"))
    containers = task.get("containers") or {}
    # 产出物清单由注册表推导:阶段声明 outputs 模板,这里只筛"阶段已完成且文件存在"。
    for stage in tracker_stages(tr):
        key = stage["key"]
        if key == "download" or not tr.is_done(key):
            continue
        # dem 阶段一次产出高程与晕渲两种,按勾选筛掉未要的那种
        if key == "dem":
            for z in ctx.levels:
                if "hillshade" in formats:
                    add(out_dir / f"{task['name']}_hillshade_z{z}.tif")
                if "geotiff" in formats:
                    add(out_dir / f"{task['name']}_dem_z{z}.tif")
            continue
        cont = containers.get(key)
        if not cont:
            sdef = STAGES.get(key)
            cont = sdef.containers[0] if (sdef and sdef.containers) else ""
        for rel in resolve_outputs(key, cont, task["name"], ctx.levels):
            add(out_dir / rel.rstrip("/"))
        # 重投影副本文件名带 EPSG 号,不在 outputs 模板里
        if key == "geotiff":
            epsg = (ctx.target_crs or "").split(":")[-1]
            if epsg and ctx.target_crs != "EPSG:4326":
                for z in ctx.levels:
                    add(out_dir / f"{task['name']}_z{z}_{epsg}.tif")
    return outs


def tracker_stages(tracker) -> list[dict]:
    """取阶段列表(隔开 tracker 内部结构,便于 _collect_outputs 遍历)。"""
    return list(getattr(tracker, "stages", []) or [])


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


# ============ 注册表与执行器的一致性校验 ============

def _assert_executors_cover() -> None:
    """确认注册表声明的每个用户可选阶段都有执行器实现。

    为什么在导入时就查:注册表是"声明能出什么格式",executors 是"实际怎么出"。
    两者若不同步,后果是界面能勾、任务能提交,但阶段跑起来空转——成果目录里
    什么都没有而任务显示成功,这类问题在运行时极难发现。宁可启动即报错。
    """
    from .formats import PIPE_RASTER, stages_for

    missing: list[str] = []
    for kind in (DataKind.RASTER_IMAGE, DataKind.RASTER_DEM):
        declared = {s.key for s in stages_for(kind, PIPE_RASTER)}
        implemented = set(_executors_for(kind))
        for key in sorted(declared - implemented):
            missing.append(f"{kind}:{key}")
    if missing:
        raise RuntimeError(
            "core.formats 声明了以下格式但 runner 未实现执行器,"
            f"勾选后会空转:{', '.join(missing)}"
        )


_assert_executors_cover()
