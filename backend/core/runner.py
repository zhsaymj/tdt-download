"""任务执行:阶段化管线(下载 → 合并 → 各切片格式),逐阶段上报进度。

被 worker 进程(进程隔离后)或 TaskQueue(过渡期,见 queue.py 的临时兼容)调用:
    async run_task(task_id, emit, should_stop)。
emit(msg: dict) 把进度上报给 worker(再经队列回传主进程广播)。
should_stop() -> "pause" | "cancel" | None:协作式停止检查,由调用方注入。
返回真值即停止;"cancel" 表示取消,其余真值(含 bool-only 闭包)按暂停处理。
不读 task_queue 单例:子进程里它是另一份副本,读不到主进程的控制状态。

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
import os
from pathlib import Path

from ..config import settings
from ..models import get_task, parse_export, update_task
from ..providers.buildings import is_building_provider
from ..providers.local_file import LocalFileProvider
from ..providers.esri_imagery import (build_esri_imagery_provider,
                                      is_esri_imagery_provider)
from ..providers.google import build_google_provider, is_google_provider
from ..providers.terrain import build_terrain_provider, is_dem_provider
from ..providers.tianditu import build_annotation_provider, build_provider
from .annotate import ANNOTATION_MAX_Z
from .containers import container_of, convert_raster
from .contour import DEFAULT_INTERVAL, extract_contours, write_contours
from .mbtiles import pack_mbtiles
from .dem import hillshade_from_dem, mosaic_dem_geotiff
from .dem_tiling import mercator_range_for_bbox, mosaic_bounds_3857
from .downloader import TileDownloader
from .formats import (CONTAINERS, GEO_GEODETIC, GEO_MERCATOR, PROVIDER_GRIDS,
                      DataKind, STAGES, download_grids_of, grid_of,
                      is_local_source, kind_of, resolve_outputs)
from .logs import logger
from .metadata import write_metadata
from .mosaic import mosaic_to_geotiff
from .osm import export_osm
from .postprocess import clip_to_geometry, crop_to_bbox, reproject_geotiff
from .progress import StageTracker
from .terrain_tiles import export_terrain, level_for_resolution, write_layer_json
from .token_pool import token_pool
from .tms import (export_tms, export_tms_from_source, source_tms_level_plan,
                  source_tms_level_plan_preserve_inputs, write_tilemapresource)
from .tile_range import download_levels, level_range
from .tiling import TILE_SIZE, range_for_bbox

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




def _anno_levels_for(levels: list[int]) -> list[int]:
    """注记实际要下载的级别:裁掉超过 ANNOTATION_MAX_Z 的部分。

    底图仍下全部选中级别(它没有级别上限),只有注记受 z18 限制。
    """
    return sorted({int(z) for z in levels if int(z) <= ANNOTATION_MAX_Z})


def _range_fn_for(provider: str, grid: str | None = None):
    """按网格返回瓦片区间函数。

    geodetic 用 4326 的 range_for_bbox;mercator(Google/Esri 影像、Esri DEM)用
    墨卡托 XYZ 的 mercator_range_for_bbox。两者不能混用 —— 混了会按错误的网格
    取瓦片,下出来的图整体错位。

    grid 显式给出时以它为准(天地图两套网格都要下,同一个 provider 名会用到两套);
    不传则回落到按 provider 推断(与改动前一致,现有调用点都只传 provider)。
    """
    if grid is None:
        grid = grid_of(provider)
    return mercator_range_for_bbox if grid == GEO_MERCATOR else range_for_bbox


def _crs_for_grid(grid: str) -> str:
    """网格对应的拼接坐标系。"""
    return "EPSG:3857" if grid == GEO_MERCATOR else "EPSG:4326"


def _tms_needs_resample(grid: str) -> bool:
    """TMS 导出是否需要重采样(而非无损直映射)。

    TMS 用的是 gdal2tiles geodetic 网格(EPSG:4326):
      - geodetic 源(天地图):与之同构,可无损直映射(export_tms)
      - mercator 源(Google/Esri):网格不同构,必须重采样
        (export_tms_from_source,以拼接图为源逐瓦片 reproject)
    走错会把 3857 瓦片当 4326 瓦片摆放,产出整体错位的瓦片包。
    """
    return grid == GEO_MERCATOR


def _ctx_grid(ctx) -> str:
    """取导出上下文的网格;缺失时回落 geodetic。

    用 getattr 而非 ctx.grid:`_ExportCtx` 是 **kw 容器,真实上下文都有该字段,
    但测试替身与旧构造不一定带 —— 而"没有网格"正等价于旧的全局 4326 行为,
    故回落到 geodetic 既向后兼容又语义正确。
    """
    return getattr(ctx, "grid", GEO_GEODETIC)


def _mbtiles_scheme_for(grid: str, stage_key: str) -> str:
    """打包 MBTiles 时的行号约定。

    取决于**瓦片目录本身**的约定,与源网格无关:
      - tms/ 目录:行号自南向北 => "tms"
      - osm/ 目录:行号自北向南 => "xyz"
    grid 参数保留是为了让调用点显式表明"已考虑过网格",避免后人误以为漏了。
    """
    return "xyz" if stage_key == "osm" else "tms"


def _build_provider_for(task, grid: str | None = None):
    """按 provider key 构造数据源实例(分发即守卫)。

    分支顺序无所谓(各 is_xxx 互斥),但必须都在 —— 漏一个会静默回落到
    build_provider 并报"暂不支持的数据源"。

    grid 只在天地图上起作用:同一个图层有两套网格(`_c`/`_w`),靠 matrix_set
    切换,而 key 会带上网格后缀(缓存路径按它分流,见 providers/tianditu.py)。
    其余数据源只有一套网格,忽略该参数。
    """
    key = task["provider"]
    if is_dem_provider(key):
        return build_terrain_provider(key)
    if is_google_provider(key):
        return build_google_provider(key, settings.google)
    if is_esri_imagery_provider(key):
        return build_esri_imagery_provider(settings.esri_imagery)
    token_src = (token_pool.use_token if token_pool.has_any()
                 else settings.tianditu.token)
    if grid is not None and key in PROVIDER_GRIDS:
        return build_provider(key, token_src, matrix_set=_matrix_set_of(grid))
    return build_provider(key, token_src)


def _matrix_set_of(grid: str) -> str:
    """网格 → 天地图 TILEMATRIXSET(`c` = EPSG:4326,`w` = EPSG:3857)。"""
    return "w" if grid == GEO_MERCATOR else "c"


def _mk_downloader(task, prov) -> TileDownloader:
    """按 provider 造一个瓦片下载器。

    抽出来是因为"某个网格的缓存路径怎么拼"只该有一处实现
    (`TileDownloader.tile_path` 用 `provider.key`,而 key 带网格后缀);
    OSM 自拼源时也要按网格读缓存,不能再抄一份路径规则(本项目为此栽过多次)。
    """
    return TileDownloader(
        prov, cache_dir=settings.cache_dir,
        concurrency=settings.download.concurrency,
        max_retries=settings.download.max_retries,
        timeout=settings.download.timeout,
        use_cache=task.get("use_cache", True))


def _grids_missing_cache(cache_dir: Path, provider_key: str, grids,
                         levels) -> list[str]:
    """缓存里**完全没有数据**的网格。

    用途:"download 阶段已完成、但要求的网格集变了"的情形。升级前建的任务只下过
    `_c`;升级后重跑 osm(或恢复一个在 osm 阶段停下的任务)时 download 已是 done,
    若不补下 `_w`,OSM 会从空缓存拼出**全零源 → 空成果 → 却报成功**
    (见 `_build_osm_source` 的护栏与最终审查的 Critical 1)。

    判据只看"该网格的最高级别目录下有没有文件",与扩展名无关 —— 不复制缓存路径
    规则(那条规则只在 `TileDownloader.tile_path` 一处)。
    """
    missing = []
    for g in grids:
        base = cache_dir / f"{provider_key}_{_matrix_set_of(g)}"
        found = False
        for z in levels:
            try:
                if next(iter(os.scandir(base / str(z))), None) is not None:
                    found = True
                    break
            except OSError:
                continue
        if not found:
            missing.append(g)
    return missing


def _osm_uses_mercator_cache(ctx) -> bool:
    """OSM 是否需要另用 `_w` 缓存拼 3857 源(而不是复用 geotiff 成果)。

    成立条件:源**两套网格都有**(天地图),而任务主网格不是 mercator ——
    此时 geotiff 成果是 4326 的,对 OSM(3857 输出)没用:

    * 拿它当源 → `export_osm` 得逐瓦片重投影,有损(设计 D4 要消除的正是这个)
    * geotiff 阶段也不必为 OSM 留存未裁剪副本(那份同样是主网格的,白占磁盘)
    """
    return (GEO_MERCATOR in PROVIDER_GRIDS.get(ctx.task["provider"], ())
            and _ctx_grid(ctx) != GEO_MERCATOR)


async def _download_all_grids(*, provider_key: str, grids, levels, bbox,
                              anno_levels, downloader_for, anno_for,
                              on_progress, should_stop,
                              global_max_level=0, buffer_rings=0) -> bool:
    """按网格逐级下载原始瓦片(与注记);返回是否被停止。

    ... 现有 docstring ...
    global_max_level: 全球底图层级上限(0=不启用),见 tile_range.level_range
    buffer_rings: 每层范围外外扩圈数(0=不外扩)
    """
    for grid in grids:
        range_fn = _range_fn_for(provider_key, grid)
        dl = downloader_for(grid)
        anno = anno_for(grid) if anno_for is not None else None
        for z in levels:
            tr = level_range(z, grid, bbox,
                             global_max_level=global_max_level,
                             buffer_rings=buffer_rings)
            _ok, _fail, stopped = await dl.download_range(
                tr, on_progress, should_stop)
            if stopped:
                return True
            if anno is not None and z in anno_levels:
                _ok, _fail, stopped = await anno.download_range(
                    tr, on_progress, should_stop)
                if stopped:
                    return True
    return False


async def run_task(task_id: str, emit, should_stop) -> None:
    """执行任务。

    should_stop():协作式停止检查,由调用方注入(worker 进程传入本地控制镜像;
    测试可传 lambda: False)。返回**真值**即需停止(所有下游消费点都按真值判断),
    其中返回 "cancel" 表示取消,其余真值(如 "pause"、True)一律按暂停处理
    —— 见 _handle_stop 的说明。
    """
    task = get_task(task_id)
    if not task:
        return

    # 三维建筑白模走独立管线(数据是矢量要素集,无瓦片行列号,不复用下载器/拼接)
    if is_building_provider(task["provider"]):
        from .runner_buildings import run_buildings_task
        return await run_buildings_task(task_id, emit, should_stop)

    # 本地文件源:不联网、不需要密钥,数据已在用户磁盘上
    local_src = None
    if is_local_source(task["provider"]):
        local_src = Path(task.get("source_path") or "")
        if not local_src.is_file():
            msg = f"源文件不存在或已移动:{local_src}"
            update_task(task_id, status="failed", message=msg)
            emit({"type": "task", "id": task_id, "status": "failed", "message": msg})
            logger.error("任务[%s] %s", task["name"], msg)
            return

    is_dem = is_dem_provider(task["provider"]) or task["provider"] == "local_dem"
    if local_src is not None:
        # 造一个轻量 provider:下游只用它取 ext / bands / key(写 tilemapresource
        # 与 metadata),不做任何下载。这样 write_tilemapresource、_write_metadata
        # 等无需到处判空。
        provider = LocalFileProvider(task["provider"], local_src)
    else:
        provider = _build_provider_for(task)
    downloader = None if local_src is not None else TileDownloader(
        provider,
        cache_dir=settings.cache_dir,
        concurrency=settings.download.concurrency,
        max_retries=settings.download.max_retries,
        timeout=settings.download.timeout,
        use_cache=task.get("use_cache", True),
    )

    # 注记要联网下载同网格的注记瓦片,本地文件源没有这个概念
    # 注记是天地图提供的透明覆盖层(cia/cva/cta),按底图类型配对。
    # DEM 无此概念、本地文件源不联网,两者都不带注记。
    # Google/Esri 支持:它们走 3857,而天地图注记有 3857 版本(cia_w),
    # 同格可直接对取(见 build_annotation_provider 的 grid 参数)。
    annotate = (task.get("annotate", False) and not is_dem
                and local_src is None)
    anno_token_src = token_pool.use_token if token_pool.has_any() else settings.tianditu.token
    # 注记的网格必须跟底图一致:行列号由底图的 range_fn 算出,网格选错会
    # 请求到另一个地方的注记(不报错,只是路网对不上影像)。
    anno_provider = (build_annotation_provider(
        task["provider"], anno_token_src, grid=grid_of(task["provider"]))
        if annotate else None)
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

    update_task(task_id, status="running",
                message="开始处理" if local_src is not None else "开始下载")
    emit({"type": "task", "id": task_id, "status": "running"})
    pending = [s["key"] for s in tracker.stages if s["status"] not in ("done", "skipped")]
    logger.info("任务[%s]开始运行 provider=%s 级别=%s 格式=%s 待执行阶段=%s",
                task["name"], task["provider"], levels, formats, pending)

    total = task["total"]
    downloaded = task.get("downloaded", 0)
    failed = task.get("failed", 0)

    # ---------- 阶段 1:下载原始瓦片 ----------
    # 本地文件源没有 download 阶段(见 models.build_stage_defs);is_done() 对
    # 不存在的阶段返回 False,故要先确认该阶段确实在阶段表里,否则会误入下载分支。
    has_download = any(s["key"] == "download" for s in tracker.stages)
    # download 阶段已 done、但**要求的网格集变了**时也要补下:升级前建的任务只下过
    # `_c`,升级后重跑 osm 时若不补下 `_w`,OSM 会从空缓存拼出空成果(最终审查
    # Critical 1)。此时把整个下载阶段重跑一遍即可 —— 已在缓存里的瓦片会被跳过,
    # 实际只补下缺的那套。
    _missing_grids = (_grids_missing_cache(settings.cache_dir, task["provider"],
                                           download_grids_of(task["provider"],
                                                             formats), levels)
                      if has_download else [])
    if has_download and (not tracker.is_done("download") or _missing_grids):
        if _missing_grids:
            logger.info("任务[%s] 缓存缺网格 %s,补下下载阶段", task["name"],
                        ",".join(_missing_grids))
        # 进度分母必须与**本次实际要下的量**一致。分母是建任务时算好落库的,而
        # 运行期实际量可能更大(最典型:补下另一个网格时那套瓦片没算进去)——
        # 不重算就会出现"进度到 100% 却还在下载、且下载数大于总数"(需求39)。
        # 用与建任务同一个函数(core.tile_estimate 是唯一判定处),两处不会再漂。
        from .tile_estimate import tile_total
        _want_total = tile_total(task["provider"], formats, bbox, levels,
                                 annotate=annotate,
                                 global_max_level=int(task.get(
                                     "global_max_level", 0) or 0),
                                 buffer_rings=int(task.get("buffer_rings", 1))
                                 if int(task.get("global_max_level", 0) or 0) > 0 else 0)
        if _want_total != total:
            logger.info("任务[%s] 下载量按当前网格重算:%d → %d 张",
                        task["name"], total, _want_total)
            total = _want_total
            # 计数也要清零:本阶段会重跑一遍(已缓存的瓦片按 ok 计入),从 0 起算
            # 才与新的分母同步;否则界面会在下一次限流落库前一直显示旧的大数字。
            update_task(task_id, total=total, downloaded=0, failed=0)
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
        # 注记层级随下载层级走(全球段也要下注记)
        # anno_levels 在最终的调用点按 dl_levels 重新定义

        # 按网格下载(设计 D3):天地图两套网格都要下时(tms+osm 同选),每个网格
        # 配自己的 provider / 区间函数 / 下载器 —— `_c` 与 `_w` 的行号语义不同,
        # 混用会按错误网格取瓦片、下出来的图整体错位,而且不报错。
        # 其余数据源只有一套网格,grids 就一项,行为与改动前完全一致。
        default_grid = grid_of(task["provider"])
        grids = download_grids_of(task["provider"], formats)

        dl_by_grid = {default_grid: downloader}
        anno_by_grid = {default_grid: anno_downloader}
        for g in grids:
            if g == default_grid:
                continue
            dl_by_grid[g] = _mk_downloader(task, _build_provider_for(task, g))
            if anno_downloader is not None:
                anno_prov = build_annotation_provider(
                    task["provider"], anno_token_src, grid=g)
                anno_by_grid[g] = _mk_downloader(task, anno_prov) if anno_prov else None

        # 全球底图 + 边缘缓冲(设计 2026-09-30)
        global_max = int(task.get("global_max_level", 0) or 0)
        buffer_rings = (1 if task.get("buffer_rings", 1) is None
                        else int(task.get("buffer_rings", 1)))
        dl_levels = download_levels(levels, global_max)
        anno_levels = _anno_levels_for(dl_levels)

        stopped = await _download_all_grids(
            provider_key=task["provider"], grids=grids, levels=dl_levels, bbox=bbox,
            anno_levels=anno_levels,
            global_max_level=global_max, buffer_rings=buffer_rings,
            downloader_for=lambda g: dl_by_grid[g],
            anno_for=(lambda g: anno_by_grid.get(g)) if anno_downloader else None,
            on_progress=on_progress, should_stop=should_stop)

        if stopped:
            return _handle_stop(task_id, tracker, "download", downloaded,
                                failed, total, emit, should_stop)

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
        # 数据源网格("geodetic" / "mercator")。各导出阶段据此选瓦片数学与拼接 crs。
        grid=grid_of(task["provider"]),
        tracker=tracker, should_stop=should_stop, cur_stage="",
        # 延后到全部阶段结束后再做的容器转换(见 _stage_geotiff 的说明)
        deferred_convert=[],
        # DEM 的裁切是"裁到选区外接矩形"(窗口裁剪、缩小尺寸),与影像按几何遮罩
        # 裁边界是两回事:DEM 成果按瓦片区间出图,边界是瓦片网格边界,低层级能超出
        # 选区数倍。环形/飞地这类复杂几何对高程没有意义,故只用外接矩形。
        clip_bbox=bool(task.get("clip")) and is_dem,
        # 本地文件输入源的原始路径(不下载,直接读用户磁盘上的文件)。
        # 各阶段据此改走"读文件"而非"拼瓦片"的取源路径。
        local_src=local_src,
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
            return _handle_stop(task_id, tracker, key, downloaded, failed,
                                total, emit, should_stop)
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
    # 本地源没有下载计数,报"0 张成功"会让人以为出错了
    is_local = local_src is not None
    if any_failed:
        msg = ("部分完成(有导出阶段失败,可单独重试)" if is_local else
               f"部分完成:{downloaded} 张成功,{failed} 张失败(有导出阶段失败,可单独重试)")
        update_task(task_id, status="failed", message=msg,
                    output_path=str(out_dir), downloaded=downloaded, failed=failed)
        emit({"type": "task", "id": task_id, "status": "failed",
              "message": msg, "output_path": str(out_dir), "outputs": outputs})
    else:
        msg = ("处理完成" if is_local else
               f"完成:{downloaded} 张成功,{failed} 张失败")
        update_task(task_id, status="done", message=msg,
                    output_path=str(out_dir), downloaded=downloaded, failed=failed)
        emit({"type": "task", "id": task_id, "status": "done",
              "message": msg, "output_path": str(out_dir), "outputs": outputs})


def _handle_stop(task_id, tracker, key, downloaded, failed, total, emit,
                 should_stop):
    """暂停/取消:把当前阶段标记为对应状态,落库任务状态后返回。

    停止原因取自 should_stop() 的**返回值**:返回 "cancel" 才是取消,返回
    "pause" 或纯 True(bool-only 闭包区分不了两者)一律按暂停落库。不能只看
    真假——暂停与取消都会让 should_stop() 为真,一律按取消落库会让用户点了
    「暂停」(接口已回「已暂停」)却看到「已取消」,误以为任务被丢弃。
    暂停是可恢复的一侧,拿不准时按它落库更安全。
    """
    ctrl = "cancel" if should_stop() == "cancel" else "pause"
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


def _clip_geom_of(ctx):
    """取裁切用几何:有 geometry 用它,否则(DEM 的矩形选区)用 bbox 造矩形环。

    瓦片必须对齐网格、不能像整幅图那样裁掉多余像素,故只能靠 alpha 遮罩把选区外
    设为透明——遮罩需要一个几何。DEM 任务前端不送 geometry(只裁到外接矩形),
    这里补上,让瓦片切片器与影像走同一条路。
    """
    task = ctx.task
    if not task.get("clip"):
        return None
    if ctx.geom:
        return ctx.geom
    w, s, e, n = ctx.bbox
    return {"type": "Polygon",
            "coordinates": [[[w, s], [e, s], [e, n], [w, n], [w, s]]]}


# ============ 影像管线阶段 ============

def _resample_to_level(ctx, src_path: Path, dst_path: Path, tr, on_row=None) -> Path:
    """把本地栅格重采样成"该瓦片区间对齐"的 EPSG:4326 GeoTIFF。

    为什么要对齐瓦片网格而不是直接拷源文件:下游 tms/osm 切片、以及"每级一张"的
    成果约定都建立在"图的四至等于瓦片区间四至"之上(见 tms.export_tms_from_source
    按 tile_bounds 反算窗口)。直接用源文件的四至,切片时边缘会错位半个瓦片。

    重采样用 bilinear;源与目标坐标系不同时由 WarpedVRT 顺带完成重投影。
    逐块写出并回调 on_row,大图不必整份进内存。
    """
    import rasterio
    from rasterio.enums import ColorInterp, Resampling
    from rasterio.transform import from_bounds as _from_bounds
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import Window

    west, south, east, north = tr.mosaic_bounds()
    width, height = tr.cols * TILE_SIZE, tr.rows * TILE_SIZE
    transform = _from_bounds(west, south, east, north, width, height)

    with rasterio.open(src_path) as src:
        alpha_indexes = {
            i for i, ci in enumerate(src.colorinterp, start=1)
            if ci == ColorInterp.alpha
        }
        alpha_index = min(alpha_indexes) if alpha_indexes else None
        data_indexes = [
            i for i in range(1, src.count + 1)
            if i not in alpha_indexes
        ] or [1]
        # RGBA/灰度+alpha 本地源已经有透明通道,GDAL 不允许再 add_alpha。
        # 统一写成显式 alpha 波段,桌面查看器和下游 TMS/OSM 都能识别透明区。
        add_alpha = not alpha_indexes
        with WarpedVRT(src, crs="EPSG:4326", transform=transform,
                        width=width, height=height,
                        resampling=Resampling.bilinear,
                        add_alpha=add_alpha) as vrt:
            profile = vrt.profile.copy()
            # 本地源图通常不会刚好落在瓦片网格边界上。WarpedVRT 会把源图
            # 覆盖不到的目标像素填成 0；如果只写 RGB 而不写 mask，QGIS / 下游
            # 切片会把这些 0 当成有效黑像素，形成上/右/下黑边。
            profile.pop("nodata", None)
            profile.update(driver="GTiff", tiled=True, blockxsize=256,
                           blockysize=256, compress="deflate",
                           count=len(data_indexes) + 1,
                           photometric="RGB" if len(data_indexes) >= 3 else "MINISBLACK",
                           BIGTIFF="IF_SAFER")
            # 行块高度取一屏瓦片,既控制内存也让进度回调有合理粒度
            step = TILE_SIZE
            with rasterio.open(dst_path, "w", **profile) as dst:
                total = max(1, (height + step - 1) // step)
                for i, y0 in enumerate(range(0, height, step), start=1):
                    _check_stop(ctx)
                    h = min(step, height - y0)
                    win = Window(0, y0, width, h)
                    data = vrt.read(indexes=data_indexes, window=win)
                    if add_alpha:
                        alpha = vrt.read(len(data_indexes) + 1, window=win)
                    else:
                        alpha = vrt.read(alpha_index, window=win)
                    data[:, alpha == 0] = 0
                    dst.write(data, indexes=list(range(1, len(data_indexes) + 1)),
                              window=win)
                    dst.write(alpha, len(data_indexes) + 1, window=win)
                    if on_row:
                        on_row(i, total)
                if len(data_indexes) >= 3:
                    dst.colorinterp = [
                        ColorInterp.red, ColorInterp.green,
                        ColorInterp.blue, ColorInterp.alpha,
                    ]
                else:
                    dst.colorinterp = [ColorInterp.gray, ColorInterp.alpha]
    return dst_path


def _stage_geotiff(ctx) -> list[str]:
    """每个选中级别各合并导出一张 {任务名}_z{级别}.tif。

    进度按"瓦片行"细粒度上报(所有层级的行数之和作分母),避免高层级卡死进度条。
    """
    task = ctx.task
    anno_path_fn = ctx.anno_downloader.tile_path if ctx.anno_downloader is not None else None
    levels = ctx.levels
    # 按网格取区间与拼接坐标系:天地图出 4326,Google/Esri 出 3857
    range_fn = _range_fn_for(task["provider"])
    mosaic_crs = _crs_for_grid(_ctx_grid(ctx))
    trs = {z: range_fn(*ctx.bbox, z) for z in levels}
    total_rows = sum(trs[z].rows for z in levels)
    # 裁剪 + 要出 OSM 时,需在裁剪**前**把 OSM 要用到的那几级留存一份未裁剪源
    # (OSM 自带几何遮罩、需要未裁剪源才能精确切边;用裁过的源会在几何边缘产生暗边)。
    # 要哪几级由 OSM 的断层策略决定 —— 与 TMS 同一套计划,不再只留最高级(需求38-2)。
    _need_osm = "osm" in parse_export(task.get("export", "geotiff"))
    _clipping = bool(task.get("clip") and ctx.geom)
    # 但两套网格都有的源(天地图)是例外:它的 OSM 源要另用 _w 缓存拼 3857 的,
    # 这里留存的主网格副本对它没用 —— 白占磁盘与时间,故不留。
    _osm_keep = ({sz for sz, _ in _source_tms_plan_for_task(ctx)}
                 if (_need_osm and _clipping and ctx.local_src is None
                     and not _osm_uses_mercator_cache(ctx)) else set())
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

        # 主文件的坐标系跟随数据源网格:天地图出 4326,Google/Esri 出 3857。
        # 下游 tms/osm 会据此决定是否需要重投影。
        # 裁剪在主文件坐标系下做;几何是 WGS84,3857 主文件需先转换
        # (见 postprocess.clip_to_geometry)。
        geotiff = ctx.out_dir / f"{task['name']}_z{z}.tif"
        if ctx.local_src is not None:
            # 本地源:没有瓦片可拼,直接由源文件重采样出该级别的图。
            # 仍按级别出多张:用户可能要一套不同分辨率的成果,且下游 tms/osm
            # 的复用逻辑也依赖"最高级那张"的存在。
            _resample_to_level(ctx, ctx.local_src, geotiff, tr, on_row)
        else:
            mosaic_to_geotiff(ctx.provider, settings.cache_dir, tr, geotiff,
                              ctx.downloader.tile_path, anno_path_fn,
                              on_row=on_row, crs=mosaic_crs)

        # 裁剪前把 OSM 要用的那几级未裁剪图留一份(见上面 _osm_keep 的说明)。
        if z in _osm_keep:
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


def _count_tiles(root: Path) -> int:
    """数一个瓦片目录下的瓦片文件数(不存在则 0)。"""
    if not root.exists():
        return 0
    return sum(1 for p in root.rglob("*") if p.is_file())


def _tms_output_looks_empty(expected: int, after: int) -> bool:
    """TMS 阶段是否"本该有产出,结束时目录却是空的"。

    抽成纯函数以便单测:实测踩到的坑是墨卡托源(3857)被直接喂给
    export_tms_from_source —— 它用 4326 网格枚举瓦片再与源图 bounds 求交,
    源是米制时交集恒为空,一张都没切出来,阶段却报 "8/8 张"、状态 done,
    成果目录全空。用户要到打开目录才发现,属于最难排查的一类问题。

    判据是"结束时目录为空"而非"本次没新增":断点续切/重试时瓦片已存在,
    重切会原地覆盖、文件数不增,按"没新增"判会**误报失败**。
    """
    return expected > 0 and after == 0


def _tms_from_planned_sources_guarded(ctx, tms_dir: Path, clip_geom, plan,
                                      source_for_level) -> list[str]:
    """包一层:产出为空时报错而不是静默成功(判据见 _tms_output_looks_empty)。"""
    out = _tms_from_planned_sources(ctx, tms_dir, clip_geom, plan,
                                    source_for_level)
    after = _count_tiles(tms_dir)
    expected = sum(_tms_tile_count(ctx.bbox, lv) for _, lv in plan)
    if _tms_output_looks_empty(expected, after):
        raise RuntimeError(
            f"TMS 切片产出为空:计划 {expected} 张瓦片,结束时目录里没有任何文件。"
            f"常见原因是源图坐标系与 geodetic(4326)网格不匹配 —— "
            f"源图必须是 EPSG:4326。")
    return out


def _stage_tms(ctx) -> list[str]:
    """输出 gdal2tiles geodetic TMS 瓦片包 + tilemapresource.xml。

    进度按瓦片数细粒度上报。
    """
    task = ctx.task
    anno_path_fn = ctx.anno_downloader.tile_path if ctx.anno_downloader is not None else None
    clip_geom = _clip_geom_of(ctx)
    levels = ctx.levels
    ctx.tracker.start("tms", total=1, message="切 TMS 瓦片")
    tms_dir = ctx.out_dir / "tms"

    # DEM 走另一条路:export_tms 是"从缓存逐张搬运瓦片",而 DEM 缓存里是 3857 网格的
    # LERC 编码瓦片,既非 4326 行列号也不是图片,搬不过来。故先渲染成 4326 RGB 源,
    # 再按 TMS 网格重新切分(_export_tms_from_source)。
    if ctx.is_dem:
        return _tms_from_dem(ctx, tms_dir, clip_geom)

    # 本地影像源同理:没有瓦片缓存可搬,改由源文件重采样切分。
    # 源优先用 geotiff 阶段已产出的最高级图(它已对齐瓦片网格),否则直接用原文件。
    if ctx.local_src is not None:
        return _tms_from_source_file(ctx, tms_dir, clip_geom)

    # 墨卡托源(Google/Esri 影像):瓦片是 3857,与 TMS 的 geodetic 网格不同构,
    # 不能用 export_tms 的无损直映射 —— 那会把 3857 瓦片当 4326 瓦片摆放,
    # 产出整体错位的瓦片包。必须以拼接图为源逐瓦片重采样。
    #
    # 计划用"每级各自为源、不向下补级":用户明确选择"只出已下载级别"
    # (设计 §1 非目标),故不给墨卡托源补金字塔。
    if _tms_needs_resample(_ctx_grid(ctx)):
        plan = [(z, [z])
                for z in sorted({int(v) for v in levels}, reverse=True)]
        # 源要先转 4326(export_tms_from_source 的硬要求,见该函数说明)
        return _tms_from_planned_sources_guarded(
            ctx, tms_dir, clip_geom, plan,
            lambda source_z: _mercator_raster_source(ctx, source_z))

    plan = _source_tms_plan_for_task(ctx)
    if _tms_plan_requires_source(plan):
        return _tms_from_downloaded_sources(ctx, tms_dir, clip_geom, plan)

    # 全球底图段(geodetic 源):低层级真实全球瓦片,写进同一 tms_dir。
    # 级号/行号换算走 export_tms 内部,不在外面自己算(混用即整体错位且不报错)。
    global_max = int(ctx.task.get("global_max_level", 0) or 0)
    global_bbox = (-180.0, -90.0, 180.0, 90.0)
    if global_max > 0 and _ctx_grid(ctx) == "geodetic":
        gg = [z for z in range(1, global_max + 1)]

        def on_progress_g(done, total):
            ctx.tracker.update("tms", done=done, total=total,
                               message=f"切全球底图({done}/{total} 张)")

        _, gz, _, gstop = export_tms(
            ctx.provider, ctx.downloader.tile_path, global_bbox, gg, tms_dir,
            clip_geom=None, anno_tile_path_fn=anno_path_fn,
            on_progress=on_progress_g, should_stop=ctx.should_stop)
        if gstop:
            raise _Stopped()
        if gz:
            logger.info("任务[%s] 全球底图段已输出 z%s", task["name"], gz)

    def on_progress(done, total):
        ctx.tracker.update("tms", done=done, total=total,
                           message=f"已切 {done}/{total} 张")

    _, _, tms_ext, stopped = export_tms(
        ctx.provider, ctx.downloader.tile_path, ctx.bbox, levels, tms_dir,
        clip_geom=clip_geom, anno_tile_path_fn=anno_path_fn,
        on_progress=on_progress, should_stop=ctx.should_stop)
    if stopped:
        raise _Stopped()
    merged_bbox = global_bbox if (global_max > 0 and _ctx_grid(ctx) == "geodetic") else ctx.bbox
    merged_levels = list(range(1, max(levels) + 1)) if global_max > 0 else levels
    write_tilemapresource(tms_dir, ctx.provider, task["name"],
                          merged_bbox, merged_levels, ext=tms_ext)
    return _maybe_mbtiles(ctx, "tms", tms_dir,
                    _mbtiles_scheme_for(_ctx_grid(ctx), "tms"), tms_ext)


def _maybe_mbtiles(ctx, stage_key: str, tiles_dir: Path, scheme: str,
                   tile_ext: str) -> list[str]:
    """选了 MBTiles 容器时把瓦片目录打包成单文件,否则原样返回目录。

    打包后是否保留散列瓦片目录由任务的 keep_tiles_dir 决定(默认保留):
      - 保留:目录可直接挂 HTTP 服务,mbtiles 便于分发,两者内容等价
      - 不保留:省一半磁盘。瓦片数据只存一遍
    默认保留是因为删掉不可逆——用户事后发现需要目录就得重切一遍。
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
    if ctx.task.get("keep_tiles_dir", True):
        return [str(packed), str(tiles_dir)]
    import shutil as _shutil
    _shutil.rmtree(tiles_dir, ignore_errors=True)
    return [str(packed)]


def _local_raster_source(ctx, source_z: int | None = None) -> Path:
    """本地源任务里,供切片使用的 4326 源图。

    优先用 geotiff 阶段已产出的指定级别图:它已重采样并对齐瓦片网格,切片时窗口
    换算是整数、无边缘错位。该阶段未跑(用户只勾了瓦片)时才临时生成一张。
    """
    task = ctx.task
    z = source_z if source_z is not None else max(ctx.levels)
    done = ctx.out_dir / f"{task['name']}_z{z}.tif"
    if done.exists() and done.stat().st_size > 0:
        return done
    tmp = ctx.out_dir / f".local_src_z{z}.tif"
    if tmp.exists() and tmp.stat().st_size > 0:
        return tmp

    def on_row(done_rows, total_r):
        _check_stop(ctx)
        ctx.tracker.update(ctx.cur_stage,
                           message=f"准备切片源({done_rows}/{total_r})")

    tr = range_for_bbox(*ctx.bbox, z)
    _resample_to_level(ctx, ctx.local_src, tmp, tr, on_row)
    return tmp


def _tms_tile_count(bbox, levels: list[int]) -> int:
    """按天地图 z 级统计 TMS 源图切片任务数。"""
    return sum(range_for_bbox(*bbox, z).count for z in levels)


def _source_tms_plan_for_task(ctx) -> list[tuple[int, list[int]]]:
    if ctx.task.get("tms_source_strategy") == "preserve_inputs":
        return source_tms_level_plan_preserve_inputs(ctx.levels)
    return source_tms_level_plan(ctx.levels)


def _tms_plan_requires_source(plan: list[tuple[int, list[int]]]) -> bool:
    """判断 TMS 计划是否需要从 GeoTIFF 源重切,而不能原始瓦片直拷。"""
    return any(target_levels != [source_z]
               for source_z, target_levels in plan)


def _tms_from_planned_sources(ctx, tms_dir: Path, clip_geom,
                              plan: list[tuple[int, list[int]]],
                              source_for_level) -> list[str]:
    """按“源层级 → 输出层级列表”计划切 TMS,供本地/在线影像共用。"""
    total_tiles = max(sum(_tms_tile_count(ctx.bbox, lv) for _, lv in plan), 1)
    done_base = 0
    exported_levels: set[int] = set()
    tms_ext = "png"

    def on_progress(done, total):
        _check_stop(ctx)
        current = min(done_base + done, total_tiles)
        ctx.tracker.update("tms", done=current, total=total_tiles,
                           message=f"切 TMS 瓦片({current}/{total_tiles} 张)")

    for source_z, levels in plan:
        src = source_for_level(source_z)
        _, tms_levels, tms_ext, stopped = export_tms_from_source(
            src, ctx.bbox, levels, tms_dir, clip_geom=clip_geom,
            on_progress=on_progress, should_stop=ctx.should_stop,
            fill_to_tms_zero=False)
        exported_levels.update(tms_levels)
        done_base += _tms_tile_count(ctx.bbox, levels)
        if stopped:
            raise _Stopped()
    write_tilemapresource(tms_dir, ctx.provider, ctx.task["name"],
                          ctx.bbox, sorted(exported_levels), ext=tms_ext)
    return _maybe_mbtiles(ctx, "tms", tms_dir,
                    _mbtiles_scheme_for(_ctx_grid(ctx), "tms"), tms_ext)


def _downloaded_raster_source(ctx, source_z: int) -> Path:
    """在线影像任务里,供补金字塔用的指定级别源图(网格跟随**任务主网格**)。

    ⚠️ 范围与 crs 都必须按任务主网格取:缓存路径用的是主网格的 key
    (`ctx.downloader.tile_path`),按 4326 行号去读 3857 命名的缓存会**不报错、
    静默拼出别处的像素**(两套网格列号相同、行号不同,文件都存在)—— 最终审查
    Important 5。墨卡托任务拼出的 3857 源,由调用方负责重投影成 4326。
    """
    task = ctx.task
    done = ctx.out_dir / f"{task['name']}_z{source_z}.tif"
    if done.exists() and done.stat().st_size > 0:
        return done
    tmp = ctx.out_dir / f".tms_src_z{source_z}.tif"
    if tmp.exists() and tmp.stat().st_size > 0:
        return tmp

    anno_path_fn = (ctx.anno_downloader.tile_path
                    if ctx.anno_downloader is not None else None)
    grid = _ctx_grid(ctx)
    tr = _range_fn_for(task["provider"], grid)(*ctx.bbox, source_z)

    def on_row(done_rows, total_r):
        _check_stop(ctx)
        ctx.tracker.update(ctx.cur_stage,
                           message=f"准备切片源({source_z}级 {done_rows}/{total_r}行)")

    mosaic_to_geotiff(ctx.provider, settings.cache_dir, tr, tmp,
                      ctx.downloader.tile_path, anno_path_fn,
                      on_row=on_row, crs=_crs_for_grid(grid))
    return tmp


def _tms_from_downloaded_sources(ctx, tms_dir: Path, clip_geom,
                                 plan: list[tuple[int, list[int]]]) -> list[str]:
    """在线影像出 TMS:需要补层时用各级 GeoTIFF 源分段重切。"""
    return _tms_from_planned_sources(
        ctx, tms_dir, clip_geom, plan,
        lambda source_z: _downloaded_raster_source(ctx, source_z))


def _mercator_raster_source(ctx, source_z: int) -> Path:
    """墨卡托源任务里,供 TMS 切片用的 **4326** 源图。

    ⚠️ 必须先转 4326,不能把 3857 拼接图直接喂给 export_tms_from_source:
    那个函数用 range_for_bbox(4326 网格)枚举输出瓦片,再与源图 bounds 求交集。
    源若是 3857(bounds 是米,±2e7 量级),而瓦片四至是经纬度(±180 量级),
    交集**恒为空** —— 一张都切不出来,却仍然返回成功(实测 TMS 阶段报
    "8/8 张"、状态 done,而 tms/ 目录是空的)。

    这与 DEM 的处理同源(_tms_from_dem 也是先渲染 4326 可视化源再切)。
    """
    task = ctx.task
    src = ctx.out_dir / f"{task['name']}_z{source_z}.tif"
    if not (src.exists() and src.stat().st_size > 0):
        src = _downloaded_raster_source(ctx, source_z)
    dst = ctx.out_dir / f".tms_src4326_z{source_z}.tif"
    if dst.exists() and dst.stat().st_size > 0:
        return dst
    import shutil as _shutil
    from rasterio.enums import Resampling
    _shutil.copyfile(src, dst)
    # ⚠️ 用 cubic 而非 reproject_geotiff 的默认 bilinear。这一步是墨卡托源出
    # TMS 的**必经重投影**(见上),而 TMS 是唯一能出文字标注的瓦片格式 ——
    # 双线性把细笔画抹得最狠:实测高频能量只剩 68%,用户看到"下载切片后的
    # 文字标注比原始模糊很多"(需求37)。cubic 约 80%。
    # 不用 lanczos(90%):它在高对比边缘产生振铃,文字上比略软更显眼。
    reproject_geotiff(dst, "EPSG:4326", resampling=Resampling.cubic)
    return dst


def _tms_from_source_file(ctx, tms_dir: Path, clip_geom) -> list[str]:
    """本地影像源出 TMS:由源图重采样切 geodetic 网格。"""
    return _tms_from_planned_sources(
        ctx, tms_dir, clip_geom, _source_tms_plan_for_task(ctx),
        lambda source_z: _local_raster_source(ctx, source_z))


def _tms_from_dem(ctx, tms_dir: Path, clip_geom) -> list[str]:
    """DEM 出 TMS:先渲染 4326 可视化源,再重采样切 geodetic 网格。"""
    src = _dem_visual_source(ctx)

    def on_progress(done, total):
        _check_stop(ctx)
        ctx.tracker.update("tms", done=done, total=total,
                           message=f"切 TMS 瓦片({done}/{total} 张)")

    _, tms_levels, tms_ext, stopped = export_tms_from_source(
        src, ctx.bbox, ctx.levels, tms_dir, clip_geom=clip_geom,
        on_progress=on_progress, should_stop=ctx.should_stop)
    if stopped:
        raise _Stopped()          # 保留可视化源,恢复时复用
    write_tilemapresource(tms_dir, ctx.provider, ctx.task["name"],
                          ctx.bbox, tms_levels, ext=tms_ext)
    _safe_unlink(src)
    return _maybe_mbtiles(ctx, "tms", tms_dir,
                    _mbtiles_scheme_for(_ctx_grid(ctx), "tms"), tms_ext)


def _stage_osm(ctx) -> list[str]:
    """重投影到 Web 墨卡托后切 XYZ 瓦片。源用最高级 4326 拼接图。

    进度分两段:拼源(按行)约占前 20%,切片(按瓦片)占后 80%,统一映射到 0-1000
    的内部刻度上报,保证平滑推进。
    """
    task = ctx.task
    anno_path_fn = ctx.anno_downloader.tile_path if ctx.anno_downloader is not None else None
    clip_geom = _clip_geom_of(ctx)
    z_max = max(ctx.levels)
    osm_levels = list(range(min(OSM_MIN_LEVEL, z_max), z_max + 1))
    # 进度直接用真实瓦片数上报(切片阶段),不再用 0-1000 虚拟刻度——虚拟刻度让
    # tracker 拿到的增量极小且不均匀,速率/ETA 计算会失真(速率显示 0、剩余时间乱跳)。
    ctx.tracker.start("osm", total=1, message="准备 OSM 源")

    # 本地影像源:用已重采样对齐的源图切片(复用 geotiff 阶段成果或临时生成)
    if ctx.local_src is not None and not ctx.is_dem:
        src_path = _local_raster_source(ctx)
        ctx.tracker.update("osm", message="切 OSM 瓦片")

        def on_progress_local(done, total):
            _check_stop(ctx)
            ctx.tracker.update("osm", done=done, total=total,
                               message=f"切 OSM 瓦片({done}/{total} 张)")

        osm_dir = ctx.out_dir / "osm"
        _, _, stopped = export_osm(src_path, osm_levels, ctx.bbox, osm_dir,
                                   on_progress=on_progress_local,
                                   clip_geom=clip_geom,
                                   should_stop=ctx.should_stop)
        if stopped:
            raise _Stopped()
        return _maybe_mbtiles(ctx, "osm", osm_dir,
                    _mbtiles_scheme_for(_ctx_grid(ctx), "osm"), "png")

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
        return _maybe_mbtiles(ctx, "osm", osm_dir,
                    _mbtiles_scheme_for(_ctx_grid(ctx), "osm"), "png")

    # ---- 在线影像:按与 TMS **相同的断层策略**分段切片(需求38-2)----
    # 原先一律从最高级降采样("只切最高级")。改为按 task.tms_source_strategy 规划
    # "源层级 → 输出层级",每个源层级用**自己那一级**的拼接图 —— 与 TMS 同一套策略、
    # 同一个字段(用户明确选择复用 TMS 的选项,不另开一个)。
    #
    # 分组数取决于级别是否连续(实测):
    #   级别 1..12(连续)      → 12 组,每级各用自己的源
    #   级别 18/17/16/13        → 连续高层兜底 3 组:z16 源出 z1..z16、z17/z18 各出自己
    #                             保留输入层级 4 组:z16 源出 z14..z16、z13 源出 z1..z13
    # 故它**不是**"和原先完全一致":常规的连续级别也改成了逐级取源,这正是需求38-2
    # 要的"不再只切最高级"。
    osm_dir = ctx.out_dir / "osm"
    plan = _source_tms_plan_for_task(ctx)
    floor = min(OSM_MIN_LEVEL, z_max)
    groups = [(sz, [z for z in lv if z >= floor]) for sz, lv in plan]
    groups = [(sz, lv) for sz, lv in groups if lv]
    # 进度按**全部分组**累计:分组数可能不少(级别连续时每级一组,见计划说明),
    # 逐组重置的话进度条会来回跳。总瓦片数用墨卡托 XYZ 计数 —— OSM 恒是 3857 网格,
    # 与源数据源的网格无关。
    group_totals = [sum(mercator_range_for_bbox(*ctx.bbox, z).count for z in lv)
                    for _, lv in groups]
    grand_total = max(sum(group_totals), 1)
    base_done = 0
    built: list[Path] = []          # 本次自拼的源,切完删掉
    stopped = False
    try:
        for (source_z, levels_here), total_here in zip(groups, group_totals):
            src_path, reusable = _osm_source_for_level(ctx, source_z)
            if not reusable:
                built.append(src_path)

            # 切片阶段进度:直接上报真实瓦片数(export_osm 给的 done/total 即真实
            # 瓦片计数),速率就是真实"张/秒",ETA 才有意义。
            def on_progress(done, total, _base=base_done, _z=source_z):
                _check_stop(ctx)
                ctx.tracker.update("osm", done=_base + done, total=grand_total,
                                   message=f"切 OSM 瓦片(源 z{_z},{_base + done}/{grand_total} 张)")

            _, _, stopped = export_osm(
                src_path, levels_here, ctx.bbox, osm_dir,
                on_progress=on_progress, clip_geom=clip_geom,
                should_stop=ctx.should_stop)
            if stopped:
                break
            base_done += total_here
    finally:
        # 暂停/取消时**保留**源,恢复时免重拼;正常结束才删自拼的那几份。
        # 用安全删除:删不掉不影响已切好的成果(阶段仍算成功)。
        if not stopped:
            for p in built:
                _safe_unlink(p)
    if stopped:
        raise _Stopped()
    return _maybe_mbtiles(ctx, "osm", osm_dir,
                    _mbtiles_scheme_for(_ctx_grid(ctx), "osm"), "png")


def _osm_source_for_level(ctx, z: int) -> tuple[Path, bool]:
    """OSM 切 z 这一层要用的源;返回 (路径, 是否切完保留)。

    优先用 geotiff 阶段**裁剪前**留存的未裁剪源(`.osm_src_z{z}.tif`)——OSM 需要
    未裁剪源(它自带几何遮罩、能精确切边;用裁过的源会在几何边缘产生暗边)。
    未裁剪任务下 geotiff 成果本身就是未裁剪源,直接复用。两者都没有(裁剪任务
    且没勾 geotiff)时才自拼一份。
    """
    task = ctx.task
    if _osm_uses_mercator_cache(ctx):
        # 天地图:geotiff 成果与留存副本都是**任务主网格(4326)**的,对 OSM 没用。
        # 另用 `_w` 缓存拼 3857 源 —— 于是 export_osm 的 WarpedVRT 是 3857→3857
        # 的空操作,零重投影(设计 D4)。文件名带 w 以示区分。
        mkept = ctx.out_dir / f".osm_src_w_z{z}.tif"
        if mkept.exists() and mkept.stat().st_size > 0:
            return mkept, True
        prov_w = _build_provider_for(task, GEO_MERCATOR)
        dl_w = _mk_downloader(task, prov_w)
        anno_w = None
        if ctx.anno_downloader is not None:
            tok = (token_pool.use_token if token_pool.has_any()
                   else settings.tianditu.token)
            ap = build_annotation_provider(task["provider"], tok,
                                           grid=GEO_MERCATOR)
            anno_w = _mk_downloader(task, ap) if ap else None
        return _build_osm_source(ctx, z, prov_w, dl_w.tile_path,
                                 GEO_MERCATOR, mkept, anno_w)

    kept = ctx.out_dir / f".osm_src_z{z}.tif"
    if kept.exists() and kept.stat().st_size > 0:
        return kept, True
    done = ctx.out_dir / f"{task['name']}_z{z}.tif"
    clipped = bool(task.get("clip") and ctx.geom)
    if not clipped and done.exists() and done.stat().st_size > 0:
        return done, True
    return _build_osm_source(ctx, z, ctx.provider, ctx.downloader.tile_path,
                             _ctx_grid(ctx), kept, ctx.anno_downloader)


def _build_osm_source(ctx, z: int, provider, tile_path_fn, grid: str,
                      dst: Path, anno_downloader) -> tuple[Path, bool]:
    """按指定网格拼一份 OSM 源(临时文件 + 原子改名,中途暂停留下的是 .tmp)。"""
    def on_row(done_rows, total_r):
        _check_stop(ctx)
        ctx.tracker.update("osm", message=f"拼接 OSM 源 z{z}({done_rows}/{total_r} 行)")

    # ⚠️ 范围与坐标系都要按**指定的网格**取。原先写死 range_for_bbox(4326)
    # 且不传 crs,对墨卡托源会按 4326 的行列号去读按 3857 命名的缓存 —— 整幅拼空。
    range_fn = _range_fn_for(ctx.task["provider"], grid)
    tr = range_fn(*ctx.bbox, z)
    anno_path_fn = (anno_downloader.tile_path
                    if anno_downloader is not None else None)
    tmp = dst.with_suffix(".tmp.tif")
    mosaic_to_geotiff(provider, settings.cache_dir, tr, tmp,
                      tile_path_fn, anno_path_fn,
                      on_row=on_row, crs=_crs_for_grid(grid))
    _assert_source_has_data(tmp, grid)
    tmp.replace(dst)   # 完整拼好才改名为正式源
    return dst, False


def _assert_source_has_data(path: Path, grid: str) -> None:
    """拼出来的源必须有数据 —— 全零说明该网格的缓存里一张瓦片都没有。

    **为什么必须拦**:源全零时 `export_osm` 把每张瓦片判成"无覆盖"直接跳过,
    于是产出**空目录却报成功**(最终审查 Critical 1)。触发路径很现实:升级前建的
    任务只下过 `_c`,升级后重跑 osm 阶段时 download 已是 done —— 若没补下 `_w`,
    用户拿到空目录且零报错。与需求38-2 是同一个失败形状,故改成明确失败。
    """
    import rasterio as _rio
    with _rio.open(path) as s:
        # 抽样读即可(全零判断不需要全图)
        data = s.read(1, out_shape=(min(s.height, 512), min(s.width, 512)))
    if not data.any():
        raise RuntimeError(
            f"OSM 源为空:{path.name} 拼出来全是 0 —— "
            f"{_matrix_set_of(grid)} 网格的缓存里没有瓦片。"
            f"通常是因为只重跑了 osm 阶段而没补下下载阶段(见 _grids_missing_cache)。")


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
    if ctx.local_src is not None:
        _resample_dem_to_level(ctx, ctx.local_src, tmp_dem, tr, on_row)
    else:
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


def _resample_dem_to_level(ctx, src_path: Path, dst_path: Path, tr,
                           on_row=None) -> Path:
    """把本地高程栅格重采样成"该墨卡托瓦片区间对齐"的 EPSG:3857 GeoTIFF。

    与 _resample_to_level 的差别:目标坐标系是 3857(与 DEM 下载管线一致,
    下游 terrain/contour 都按此假设取源)、重采样保持浮点不截断、dtype 统一
    float32(高程要小数,整型源也转成浮点以免后续晕渲/等高线计算掉精度)。
    """
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.transform import from_bounds as _from_bounds
    from rasterio.vrt import WarpedVRT
    from rasterio.windows import Window

    minx, miny, maxx, maxy = mosaic_bounds_3857(tr)
    width, height = tr.cols * TILE_SIZE, tr.rows * TILE_SIZE
    transform = _from_bounds(minx, miny, maxx, maxy, width, height)

    with rasterio.open(src_path) as src:
        nodata = src.nodata if src.nodata is not None else -32768.0
        with WarpedVRT(src, crs="EPSG:3857", transform=transform,
                       width=width, height=height,
                       resampling=Resampling.bilinear,
                       src_nodata=src.nodata, nodata=nodata) as vrt:
            profile = vrt.profile.copy()
            profile.update(driver="GTiff", count=1, dtype="float32",
                           nodata=nodata, tiled=True, blockxsize=256,
                           blockysize=256, compress="deflate",
                           BIGTIFF="IF_SAFER")
            step = TILE_SIZE
            with rasterio.open(dst_path, "w", **profile) as dst:
                total = max(1, (height + step - 1) // step)
                for i, y0 in enumerate(range(0, height, step), start=1):
                    _check_stop(ctx)
                    h = min(step, height - y0)
                    win = Window(0, y0, width, h)
                    dst.write(vrt.read(1, window=win).astype("float32"),
                              1, window=win)
                    if on_row:
                        on_row(i, total)
    return dst_path


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
        if ctx.local_src is not None:
            # 本地高程源:重采样到该级墨卡托网格。保持 EPSG:3857 与下载管线一致,
            # 后续 reproject_geotiff 再按 target_crs 转换。
            _resample_dem_to_level(ctx, ctx.local_src, dem_tif, tr, on_row)
        else:
            mosaic_dem_geotiff(tr, dem_tif, ctx.downloader.tile_path, on_row=on_row)
        # 裁剪放在晕渲**之后**:hillshade 用 3x3 邻域算坡度,先裁会让新边缘那一圈
        # 少了外侧邻居、坡度失真(表现为成果四周一道亮/暗边)。故先用完整数据出
        # 晕渲,再分别把两份成果裁到选区。
        if want_hillshade:
            hs_tif = ctx.out_dir / f"{task['name']}_hillshade_z{z}.tif"
            hillshade_from_dem(dem_tif, hs_tif, azimuth=az, altitude=alt, z_factor=zf)
            if ctx.clip_bbox:
                crop_to_bbox(hs_tif, ctx.bbox)
            if ctx.target_crs and ctx.target_crs not in ("EPSG:3857",):
                reproject_geotiff(hs_tif, ctx.target_crs)
            outputs.append(str(hs_tif))
        if want_geotiff:
            if ctx.clip_bbox:
                crop_to_bbox(dem_tif, ctx.bbox)
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
        if ctx.local_src is not None:
            _resample_dem_to_level(ctx, ctx.local_src, tmp_dem, tr, on_row)
        else:
            mosaic_dem_geotiff(tr, tmp_dem, ctx.downloader.tile_path,
                               build_overviews=False, on_row=on_row)
        # 等高线成果统一 WGS84(与其余矢量成果一致,前端可直读)
        reproject_geotiff(tmp_dem, "EPSG:4326")
        # 裁源而不是裁线:等高线是闭合环,裁线段会切出一堆断头线且要重新闭合;
        # 先把源裁到选区,提取出来的线自然不出界。
        if ctx.clip_bbox:
            crop_to_bbox(tmp_dem, ctx.bbox)
        src = tmp_dem
    else:
        # 复用 dem 阶段成果。它已按 clip_bbox 裁过(同一任务同一设置),故这里不再裁。
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
        if ctx.local_src is not None:
            _resample_dem_to_level(ctx, ctx.local_src, tmp_src, tr, on_row)
        else:
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
    # include_all_forms:瓦片阶段要把目录与 mbtiles 两种形态都清掉,否则改了容器
    # 重跑会留下上一次的另一种形态,成果目录里两份数据并存且其中一份是旧的。
    for rel in resolve_outputs(key, container, task["name"], ctx.levels,
                               include_all_forms=True):
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
        # 瓦片阶段两种形态可并存(keep_tiles_dir),故都查一遍;add() 只收实际存在的,
        # 没保留目录时那条自然不会进清单。
        for rel in resolve_outputs(key, cont, task["name"], ctx.levels,
                                   include_all_forms=True):
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
