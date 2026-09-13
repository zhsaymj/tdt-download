"""三维数据处理任务执行:本地 OSGB 模型 / 点云 → 3D Tiles / DSM / DEM。

阶段(按 formats.py 注册表,PIPE_3D):
  convert_3d   OSGB 目录 → 3D Tiles(b3dm),fanvanzh/3dtiles
  pc_dsm       点云 → DSM GeoTIFF(格网取最高点),PDAL
  pc_dem       点云 → DEM GeoTIFF(smrf 地面分类后 IDW 插值),PDAL
  pc_tile_3d   点云 → 3D Tiles(pnts),py3dtiles

**成果**(任务输出目录内):
    3dtiles/tileset.json + …      convert_3d:b3dm;pc_tile_3d:pnts
    {任务名}_dsm.tif / {任务名}_dem.tif
    metadata.json                 数据说明

与 runner_buildings 的差别:三维管线各阶段**相互独立**(不共享中间件),
一个阶段失败不阻断其余阶段,任务终态记 failed(可单独重试失败阶段)。

既定设计决策(勿随意更改):
1. **多文件点云的 DSM/DEM 逐文件转再 merge**:LasToDem 契约一期仅支持单
   文件输入(目录展开本来就是 runner 的职责);按文件粒度能给出 n/N 进度、
   失败定位到具体文件、文件间隙可响应取消;PDAL 对多 LAS 无内建 mosaic
   输出,rasterio.merge 是现成可靠的最后一步。单文件则直接出成果 tif。
2. **pc_tile_3d 目录输入逐文件各转一个 3dtiles/{序号}_{stem}/ 子目录**
   (序号前缀防目录递归时同名 LAS 互覆),tiles3d_output 返回主(第一个
   文件的)tileset.json。
3. **pc_tile_3d 开始前无条件清空自己的 3dtiles/ 目录**:py3dtiles 无增量
   续传、非空目录直接 FileExistsError(适配器刻意不传 --overwrite 防误清);
   点云任务的 3dtiles/ 专属本阶段,清空不伤及其他阶段。
4. **convert_3d 部分瓦片失败检测**:fanvanzh 单 Tile 失败仅打
   `failed:`/`ERROR` 日志仍 exit 0,必须用输出行钩子扫描,命中即判阶段
   失败;进度兜底 estimated_total 取「输入目录递归 .osgb 总数」。
5. **进度粒度**:pdal 无进度行 → 按文件粒度(n/N);osgb 文本进度大概率
   不命中 → parse_progress + progress_by_output_count 轮询双保险;
   py3dtiles 的 pnts 总数事先不可知 → 退化为文件粒度。
"""
from __future__ import annotations

import asyncio
import json
import math
import shutil
import threading
import time
from pathlib import Path

from ..config import settings
from ..models import get_task, update_task
from .logs import logger
from .processors.base import ProcessorCancelled
from .processors.las_to_3dtiles import LasTo3dTiles
from .processors.las_to_dem import LasToDem
from .processors.osgb_to_3dtiles import OsgbTo3dTiles
from .progress import StageTracker
from .queue import task_queue

# watcher/poll 等后台线程总开关;测试置 False 后用 ProcessorCancelled
# 确定性模拟取消,避免线程竞态。
_ENABLE_WATCHER = True


class _Stopped(Exception):
    """被暂停/取消,跳回顶层落库。"""


# ---------- 成果文件命名(集中定义,避免散落各处不一致)----------

def _out_dir(task: dict) -> Path:
    return (Path(task["output_path"]) if task.get("output_path")
            else settings.output_dir / task["id"])


def _source_path(task: dict) -> Path:
    return Path((task.get("source_path") or "").strip())


def _las_files(task: dict) -> list[Path]:
    """点云输入的 LAS/LAZ 文件清单:单文件 → [文件];目录 → 递归收集并排序。"""
    src = _source_path(task)
    if src.is_file():
        return [src]
    if src.is_dir():
        return sorted(p for p in src.rglob("*")
                      if p.is_file() and p.suffix.lower() in (".las", ".laz"))
    return []


def tiles3d_output(task: dict) -> Path:
    """3dtiles 主 tileset.json。

    osgb / 单文件点云在 3dtiles/ 根;点云目录输入时各文件各占一个子目录,
    主产物取第一个文件的(设计决策 2)。
    """
    root = _out_dir(task) / "3dtiles"
    if task.get("provider") == "local_pointcloud":
        files = _las_files(task)
        if len(files) > 1:
            # 与 _stage_pc_tile_3d 的子目录命名一致(序号前缀防同名互覆)
            return root / f"001_{files[0].stem}" / "tileset.json"
    return root / "tileset.json"


def dem_output(task: dict) -> Path:
    return _out_dir(task) / f"{task['name']}_dem.tif"


def dsm_output(task: dict) -> Path:
    return _out_dir(task) / f"{task['name']}_dsm.tif"


# ---------- 各阶段 ----------

def _stage_convert_3d(ctx) -> list[str]:
    """OSGB 目录 → 3D Tiles(b3dm),整目录一次转换(fanvanzh 不支持增量)。"""
    task = ctx.task
    key = "convert_3d"
    src = _source_path(task)
    if not src.is_dir():
        raise RuntimeError(f"输入不是 OSGB 目录:{src}")
    proc = OsgbTo3dTiles(settings.tools.tiles3d_exe)
    ok, msg = proc.check_available(settings)
    if not ok:
        raise RuntimeError(msg)
    for w in proc.preflight(src):
        logger.warning("任务[%s] %s", task["name"], w)

    # 进度分母:递归 .osgb 总数(每个节点约产出一个 b3dm,是可得的最接近代理)
    estimated_total = sum(1 for _ in src.rglob("*.osgb"))
    out_dir = ctx.out_dir / "3dtiles"
    # 该阶段无增量语义(fanvanzh 整目录一次转换,对非空输出目录的行为未验证),
    # 重跑前无条件清本阶段产物目录——与 _stage_pc_tile_3d 对齐;只清 3dtiles/,
    # 不误伤 dem/dsm tif(本阶段也不产生它们)。
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    ctx.tracker.start(key, total=estimated_total,
                      message=f"转换 OSGB(约 {estimated_total} 个节点)")

    # fanvanzh 单 Tile 失败只打 `failed:`/`ERROR` 日志仍 exit 0(适配器契约),
    # 必须扫描输出行才能发现部分失败。计数与样本分离:海量错误行时只留
    # 前几行样本,不无限攒列表。
    bad_count = 0
    bad_samples: list[str] = []

    def _scan(line: str):
        nonlocal bad_count
        low = line.lower()
        if "failed:" in low or "error" in low:
            bad_count += 1
            if len(bad_samples) < 5:
                bad_samples.append(line.strip())
            logger.warning("任务[%s] convert_3d 输出命中失败行:%s",
                           task["name"], line[:200])
        else:
            logger.debug("3dtiles|%s", line)

    def _on_progress(p: float):
        ctx.tracker.update(key, done=min(int(p * estimated_total),
                                         estimated_total))

    # 文本进度大概率全程不命中,另起线程按产物计数轮询兜底(设计决策 5)。
    # 用模块级类名而非 type(proc):测试 patch 的是模块内的类引用。
    poll_stop = threading.Event()

    def _poll():
        try:
            while not poll_stop.wait(1.0):
                p = OsgbTo3dTiles.progress_by_output_count(out_dir,
                                                           estimated_total)
                if p is not None:
                    _on_progress(p)
        except Exception:  # 扫盘竞态等异常记日志,不让线程静默死亡
            logger.exception("任务[%s] convert_3d 进度轮询线程异常",
                             task["name"])

    poll = threading.Thread(target=_poll, daemon=True,
                            name=f"convert3d-poll-{task['id'][:8]}")
    poll.start()
    try:
        proc.run(proc.build_cmd(input_dir=src, out_dir=out_dir),
                 out_dir=out_dir, cancel_event=ctx.cancel_event,
                 on_progress=_on_progress,
                 on_stdout_line=_scan, on_stderr_line=_scan)
    finally:
        poll_stop.set()
        poll.join(timeout=2)

    if bad_count:
        raise RuntimeError(
            f"convert_3d 部分瓦片转换失败(命中 {bad_count} 行错误日志),"
            f"如:{bad_samples[0][:120]}")
    tileset = out_dir / "tileset.json"
    if not tileset.is_file():
        raise RuntimeError(f"产物缺失:{tileset.name}")
    return [str(out_dir)]


def _estimate_resolution(proc: LasToDem, path: Path) -> tuple[float, str]:
    """pc_resolution=0 时按点云密度粗估分辨率:sqrt(bbox 面积 / 点数)。

    失败(预检不可用/头部缺数据)回落 1.0 米并在消息里注明,不阻断任务。
    """
    try:
        info = proc.preflight(str(path), cfg=settings)
        points = float(info.get("points") or 0)
        bbox = info.get("bbox") or {}
        area = abs(float(bbox.get("maxx", 0)) - float(bbox.get("minx", 0))) \
            * abs(float(bbox.get("maxy", 0)) - float(bbox.get("miny", 0)))
        if points > 0 and area > 0:
            res = round(max(math.sqrt(area / points), 0.05), 3)
            return res, f"按点云密度自动估算分辨率 {res} 米"
    except Exception as e:
        logger.warning("点云预检估算分辨率失败:%s", e)
    return 1.0, "分辨率自动估算失败,回落 1.0 米"


def _merge_tifs(parts: list[Path], out_tif: Path) -> None:
    """把多幅分幅 tif 用 rasterio.merge 拼成一幅成果 tif。

    dst_path 模式逐窗口边算边落盘,不一次性把全部分幅读进内存(大场景
    防 OOM);BIGTIFF/LZW/tiled 与 dem.py 的工程实践对齐,防成果超 4GB。
    """
    import rasterio
    from rasterio.merge import merge

    srcs = [rasterio.open(p) for p in parts]
    try:
        profile = srcs[0].profile.copy()
        # 显式 256 块:tiled 要求块尺寸是 16 的倍数,缺省时 GDAL 可能取
        # 源图尺寸当块大小(小图非 16 倍数直接报错)
        profile.update(BIGTIFF="YES", compress="LZW", tiled=True,
                       blockxsize=256, blockysize=256)
        merge(srcs, dst_path=str(out_tif), dst_kwds=profile)
    finally:
        for s in srcs:
            s.close()


def _run_pc_raster(ctx, key: str, kind: str, out_tif: Path) -> list[str]:
    """pc_dsm/pc_dem 共用:逐 LAS 文件经 PDAL 转 tif,多文件再 merge 成一幅。

    多文件逐文件转再 merge 而非目录一次转的理由见模块 docstring 设计决策 1。
    """
    task = ctx.task
    proc = LasToDem(settings.tools.pdal_exe)
    ok, msg = proc.check_available(settings)
    if not ok:
        raise RuntimeError(msg)
    files = _las_files(task)
    if not files:
        raise RuntimeError(f"输入中没有 LAS/LAZ 文件:{_source_path(task)}")

    crs = (task.get("pc_crs") or "").strip()
    resolution = float(task.get("pc_resolution") or 0.0)
    note = ""
    if resolution <= 0:
        resolution, note = _estimate_resolution(proc, files[0])
        logger.info("任务[%s] %s", task["name"], note)

    ctx.tracker.start(key, total=len(files),
                      message=note or f"{kind.upper()} 转换 {len(files)} 个文件")
    single = len(files) == 1
    parts_dir = ctx.out_dir / f"_{kind}_parts"
    produced: list[Path] = []
    for i, f in enumerate(files, 1):
        if ctx.should_stop():
            raise _Stopped()
        # 枚举序号前缀:目录递归收集时不同子目录的同名 LAS 不互相覆盖
        out = out_tif if single else parts_dir / f"{i:03d}_{f.stem}.tif"
        out.parent.mkdir(parents=True, exist_ok=True)
        proc.run_pipeline(
            input=str(f), output=str(out), kind=kind, resolution=resolution,
            crs=crs, cancel_event=ctx.cancel_event)
        for w in getattr(proc, "last_warnings", None) or []:
            logger.warning("任务[%s] %s", task["name"], w)
            ctx.tracker.update(key, message=w)
        produced.append(out)
        ctx.tracker.update(key, done=i,
                           message=f"已转换 {i}/{len(files)}:{f.name}")

    if not single:
        ctx.tracker.update(key, message="合并分幅 tif")
        _merge_tifs(produced, out_tif)
        shutil.rmtree(parts_dir, ignore_errors=True)
    if not (out_tif.is_file() and out_tif.stat().st_size > 0):
        raise RuntimeError(f"产物缺失:{out_tif.name}")
    return [str(out_tif)]


def _stage_pc_dsm(ctx) -> list[str]:
    return _run_pc_raster(ctx, "pc_dsm", "dsm", dsm_output(ctx.task))


def _stage_pc_dem(ctx) -> list[str]:
    return _run_pc_raster(ctx, "pc_dem", "dem", dem_output(ctx.task))


def _stage_pc_tile_3d(ctx) -> list[str]:
    """LAS/LAZ → 3D Tiles(pnts)。多文件各转一个 3dtiles/{序号}_{stem}/ 子目录。

    py3dtiles 无增量续传、非空目录直接 FileExistsError,故开始前无条件清空
    自己的 3dtiles/ 目录(设计决策 3)。
    """
    task = ctx.task
    key = "pc_tile_3d"
    proc = LasTo3dTiles(settings.tools.py3dtiles_python)
    ok, msg = proc.check_available(settings)
    if not ok:
        raise RuntimeError(msg)
    files = _las_files(task)
    if not files:
        raise RuntimeError(f"输入中没有 LAS/LAZ 文件:{_source_path(task)}")

    tiles_root = ctx.out_dir / "3dtiles"
    shutil.rmtree(tiles_root, ignore_errors=True)
    tiles_root.mkdir(parents=True, exist_ok=True)

    ctx.tracker.start(key, total=len(files),
                      message=f"切片 {len(files)} 个点云文件")

    # SRS 接线(计划回写必办):pc_crs 为 EPSG 码 → --srs_in <crs>
    # --srs_out 4978(3D Tiles 规范要求 ECEF);local/空 → 不传 srs 参数,
    # 产物保持原坐标,Cesium 落点大概率错误,必须警告用户。
    crs = (task.get("pc_crs") or "").strip()
    if crs and crs.lower() != "local":
        srs_in, srs_out = crs, "4978"
    else:
        srs_in = srs_out = ""
        note = ("pc_crs=local:产物保持原始本地坐标,未转 ECEF,"
                "Cesium 中落点将错误"
                if crs.lower() == "local"
                else "未指定 pc_crs:产物保持 LAS 原始坐标,未转 ECEF,"
                     "Cesium 中落点将错误")
        logger.warning("任务[%s] %s", task["name"], note)
        ctx.tracker.update(key, message=note)

    single = len(files) == 1
    for i, f in enumerate(files, 1):
        if ctx.should_stop():
            raise _Stopped()
        # 序号前缀与 tiles3d_output 的主产物推导保持一致(防同名互覆)
        out_sub = tiles_root if single else tiles_root / f"{i:03d}_{f.stem}"
        out_sub.mkdir(parents=True, exist_ok=True)
        # pnts 总数事先不可知(取决于点数与八叉树深度),进度退化为文件粒度
        cmd = proc.build_cmd(input_file=str(f), out_dir=str(out_sub),
                             srs_in=srs_in, srs_out=srs_out)
        proc.run(cmd, out_dir=str(out_sub), cancel_event=ctx.cancel_event,
                 on_progress=lambda p: ctx.tracker.update(
                     key, message=f"当前文件 {p * 100:.0f}%"))
        ctx.tracker.update(key, done=i,
                           message=f"已切片 {i}/{len(files)}:{f.name}")

    main = tiles3d_output(task)
    if not main.is_file():
        raise RuntimeError(f"产物缺失:{main.relative_to(ctx.out_dir)}")
    return [str(tiles_root)]


# ---------- 顶层调度 ----------

_EXECUTORS = {
    "convert_3d": _stage_convert_3d,
    "pc_dsm": _stage_pc_dsm,
    "pc_dem": _stage_pc_dem,
    "pc_tile_3d": _stage_pc_tile_3d,
}


class _Ctx:
    def __init__(self, **kw):
        self.__dict__.update(kw)


async def run_task(task_id: str, emit) -> None:
    """三维数据处理任务主流程。由 queue 按 provider 分发进来。"""
    task = get_task(task_id)
    if not task:
        return

    out_dir = _out_dir(task)
    out_dir.mkdir(parents=True, exist_ok=True)

    tracker = StageTracker(task_id, task.get("stages") or [], emit,
                           task_name=task.get("name") or task_id)

    def should_stop() -> bool:
        return task_queue.control_of(task_id) in ("pause", "cancel")

    # BaseProcessor.run 只认 threading.Event;watcher 线程把队列控制标志
    # 桥接给它(测试关掉 _ENABLE_WATCHER 后用 ProcessorCancelled 模拟)。
    cancel_event = threading.Event()
    watch_stop = threading.Event()

    def _watch():
        while not watch_stop.wait(0.5):
            if should_stop():
                cancel_event.set()
                return

    watcher = None
    if _ENABLE_WATCHER:
        watcher = threading.Thread(target=_watch, daemon=True,
                                   name=f"runner3d-watch-{task_id[:8]}")

    ctx = _Ctx(task=task, out_dir=out_dir, tracker=tracker,
               should_stop=should_stop, cancel_event=cancel_event)

    update_task(task_id, status="running", message="开始三维数据处理")
    emit({"type": "task", "id": task_id, "status": "running"})
    logger.info("任务[%s]开始运行(三维数据) 待执行阶段=%s",
                task["name"], tracker.pending_keys())

    any_failed = False
    try:
        # start 放进 try:此后任何异常都经 finally 停掉 watcher,不泄漏线程
        if watcher is not None:
            watcher.start()
        for stage in list(tracker.stages):
            key = stage["key"]
            if key not in _EXECUTORS or tracker.is_done(key):
                continue
            label = stage.get("label", key)
            if stage.get("reset"):
                _clear_stage_output(ctx, key)
                stage.pop("reset", None)
            logger.info("任务[%s] 阶段[%s] 开始", task["name"], label)
            t0 = time.monotonic()
            try:
                await asyncio.to_thread(_EXECUTORS[key], ctx)
                tracker.finish(key)
                logger.info("任务[%s] 阶段[%s] 完成,用时 %.1fs",
                            task["name"], label, time.monotonic() - t0)
            except (_Stopped, InterruptedError, ProcessorCancelled):
                # ProcessorCancelled 来自适配层对 cancel_event 的响应,语义同 _Stopped
                logger.info("任务[%s] 阶段[%s] 被暂停/取消", task["name"], label)
                return _handle_stop(task_id, tracker, key, emit)
            except Exception as e:
                any_failed = True
                tracker.fail(key, message=str(e)[:200])
                logger.exception("任务[%s] 阶段[%s] 失败:%s",
                                 task["name"], label, e)
                # 三维管线各阶段相互独立,失败一个不影响其余,继续跑
                continue

        # 按磁盘实际情况汇总成果(单阶段重试时也能重建完整清单)
        outputs = _collect_outputs(task)

        try:
            _write_metadata(ctx, outputs)
        except Exception as e:
            logger.warning("任务[%s] 写 metadata.json 失败:%s", task["name"], e)

        if any_failed:
            msg = "部分失败(可单独重试失败阶段)"
            update_task(task_id, status="failed", message=msg,
                        output_path=str(out_dir))
            emit({"type": "task", "id": task_id, "status": "failed",
                  "message": msg, "output_path": str(out_dir),
                  "outputs": outputs})
        else:
            msg = "处理完成"
            update_task(task_id, status="done", message=msg,
                        output_path=str(out_dir))
            emit({"type": "task", "id": task_id, "status": "done",
                  "message": msg, "output_path": str(out_dir),
                  "outputs": outputs})
        logger.info("任务[%s] 三维数据流程结束,%s", task["name"],
                    "有阶段失败" if any_failed else "成功")
    finally:
        watch_stop.set()
        # is_alive 守卫:start() 自身抛错时线程未启动,join 会 RuntimeError;
        # 已自行退出的线程也无需再 join
        if watcher is not None and watcher.is_alive():
            watcher.join(timeout=2)


def _handle_stop(task_id, tracker, key, emit):
    ctrl = task_queue.control_of(task_id)
    if ctrl == "cancel":
        tracker.pause(key, message="已取消")
        update_task(task_id, status="canceled", message="已取消")
        emit({"type": "task", "id": task_id, "status": "canceled",
              "message": "已取消"})
    else:
        tracker.pause(key, message="已暂停")
        update_task(task_id, status="paused", message="已暂停")
        emit({"type": "task", "id": task_id, "status": "paused",
              "message": "已暂停"})


def _clear_stage_output(ctx, key: str) -> None:
    """清空某阶段旧产出(单阶段重试/改参数重来时)。"""
    out = ctx.out_dir

    def rm(p: Path):
        try:
            if p.is_dir():
                shutil.rmtree(p, ignore_errors=True)
            elif p.exists():
                p.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("清理阶段产出失败 %s:%s", p, e)

    if key in ("convert_3d", "pc_tile_3d"):
        rm(out / "3dtiles")
    elif key == "pc_dsm":
        rm(dsm_output(ctx.task))
        rm(out / "_dsm_parts")
    elif key == "pc_dem":
        rm(dem_output(ctx.task))
        rm(out / "_dem_parts")
    logger.info("任务[%s] 阶段[%s] 旧产出已清空(重来)", ctx.task["name"], key)


def _collect_outputs(task: dict) -> list[str]:
    """扫盘汇总当前全部成果路径(供 metadata 与前端展示)。"""
    outs: list[str] = []
    for p in (_out_dir(task) / "3dtiles", dem_output(task), dsm_output(task)):
        if p.exists():
            outs.append(str(p))
    return outs


def _write_metadata(ctx, outputs: list[str]) -> None:
    """写三维数据成果的 metadata.json(结构与建筑管线保持一致的字段风格)。"""
    from datetime import datetime

    task = ctx.task
    crs = (task.get("pc_crs") or "").strip()
    meta = {
        "task_id": task["id"],
        "name": task["name"],
        "provider": task["provider"],
        "data_type": ("mesh_osgb_3dtiles" if task["provider"] == "local_osgb"
                      else "pointcloud"),
        "source_path": task.get("source_path") or "",
        # 点云任务的 CRS 处理与栅格分辨率(osgb 任务无此二项,为 None)
        "pc_crs": crs or None,
        "pc_resolution": float(task.get("pc_resolution") or 0.0) or None,
        "products": {
            "tiles_3d": ({
                "path": tiles3d_output(task).relative_to(ctx.out_dir).as_posix(),
                "format": ("3D Tiles 1.0 (b3dm)"
                           if task["provider"] == "local_osgb"
                           else "3D Tiles 1.0 (pnts)"),
                "note": ("pc_crs 为 EPSG 码时已转 ECEF;local/未指定时保持原坐标,"
                         "Cesium 落点将错误"
                         if task["provider"] == "local_pointcloud"
                         else "Cesium 用 Cesium3DTileset.fromUrl 直接加载"),
            } if tiles3d_output(task).exists() else None),
            "dsm": ({
                "path": dsm_output(task).name,
                "format": "GeoTIFF (单波段 float32)",
                "value": "表面高程(米),格网取最高点",
            } if dsm_output(task).exists() else None),
            "dem": ({
                "path": dem_output(task).name,
                "format": "GeoTIFF (单波段 float32)",
                "value": "地面高程(米),smrf 地面分类后 IDW 插值",
            } if dem_output(task).exists() else None),
        },
        "outputs": [Path(p).name for p in outputs],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }
    (ctx.out_dir / "metadata.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
