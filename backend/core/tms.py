"""把已下载的天地图 EPSG:4326 瓦片重映射为 gdal2tiles geodetic TMS 瓦片,
并生成 tilemapresource.xml。

天地图瓦片网格与 gdal2tiles geodetic 网格同构,可无损直映射(不重采样):
  - 级号:  gdal 级 L = 天地图 z - 1(分辨率一致,z=1 ↔ L=0,单像素 0.703125°)
  - 列号:  tx = col(两者都自西向东)
  - 行号:  ty = 2^(z-1) - 1 - row(天地图行自北向南,TMS 自南向北,需翻转)

输出目录结构与示例 NaturalEarthII 一致:{L}/{tx}/{ty}.jpg
原点在左下角 (-180, -90),0 级 2x1 瓦片。
"""
from __future__ import annotations

import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil as rio_shutil
from rasterio.enums import ColorInterp
from rasterio.io import MemoryFile

from ..providers.base import TileProvider
from .annotate import composite_annotation
from .mosaic import _read_tile
from .tile_clip import prepare_geoms, tile_alpha_mask, tile_relation, _bounds_of
from .tiling import TILE_SIZE, range_for_bbox, tile_bounds

# geodetic 0 级单像素度数 = 180 / 256
BASE_UPP = 180.0 / 256.0


def _write_tile_png(path: Path, rgb: np.ndarray, alpha: np.ndarray) -> None:
    """把 RGB(3,256,256)+alpha(256,256) 写成透明 PNG(经内存 GTiff CreateCopy)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    rgba = np.concatenate([rgb[:3], alpha[np.newaxis, :, :]], axis=0)
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        with MemoryFile() as mem:
            with mem.open(driver="GTiff", height=TILE_SIZE, width=TILE_SIZE,
                          count=4, dtype="uint8") as tmp:
                tmp.write(rgba)
                tmp.colorinterp = [
                    ColorInterp.red, ColorInterp.green,
                    ColorInterp.blue, ColorInterp.alpha,
                ]
            with mem.open() as tmp:
                rio_shutil.copy(tmp, str(path), driver="PNG")


def tms_level(z: int) -> int:
    """天地图 z 级 → gdal2tiles geodetic 级号。"""
    return z - 1


def tms_row(row: int, z: int) -> int:
    """天地图行号 → TMS 行号(纵向翻转)。"""
    return (2 ** (z - 1) - 1) - row


def units_per_pixel(level: int) -> float:
    """geodetic 指定级号的单像素度数。"""
    return BASE_UPP / (2 ** level)


def _render_tms_tile(col, row, z, out_dir, out_ext, tile_path_fn,
                     clipping, annotating, geoms, clip_bounds,
                     anno_tile_path_fn) -> bool:
    """处理并写出单张 TMS 瓦片。返回 True=已写出/已存在,False=跳过(无源/在外)。

    无跨线程共享的可变状态:按文件路径读缓存瓦片,各线程独立,线程安全。
    """
    src = tile_path_fn(col, row, z)
    if not (src.exists() and src.stat().st_size > 0):
        return False
    ty = tms_row(row, z)
    dst = out_dir / str(tms_level(z)) / str(col) / f"{ty}.{out_ext}"

    # 断点续切:已存在且非空的输出瓦片直接跳过(暂停恢复不重复劳动)
    if dst.exists() and dst.stat().st_size > 0:
        return True

    # 无裁剪、无注记:原样无损复制,保留源格式
    if not clipping and not annotating:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return True

    # 裁剪模式:按瓦片地理范围与几何的关系决定 alpha 遮罩;否则全不透明
    if clipping:
        tb = tile_bounds(col, row, z)   # (west, south, east, north)
        tbounds = (tb[0], tb[1], tb[2], tb[3])
        if tile_relation(tbounds, clip_bounds) == "outside":
            return False
        gmask = tile_alpha_mask(tbounds, geoms)
        if not gmask.any():
            return False
    else:
        gmask = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)

    rgb = _read_tile(src, 3)
    if rgb is None:
        return False
    # 叠加注记:把注记瓦片按 alpha 合成到底图之上
    if annotating:
        rgb = composite_annotation(rgb, anno_tile_path_fn(col, row, z))
    _write_tile_png(dst, rgb, gmask)
    return True


def export_tms(
    provider: TileProvider,
    tile_path_fn,
    bbox: tuple[float, float, float, float],
    levels: list[int],
    out_dir: Path,
    clip_geom: dict | None = None,
    anno_tile_path_fn=None,
    on_progress=None,
    should_stop=None,
    concurrency: int | None = None,
) -> tuple[Path, list[int], str, bool]:
    """把缓存中的天地图瓦片按 TMS 规则输出到 out_dir(多线程)。

    clip_geom: 可选 geojson 几何(WGS84)。提供时按其边界裁剪——
      瓦片完全在外→跳过;完全在内→无损复制;相交→遮罩后输出透明 PNG。
      裁剪模式下统一输出 PNG(因需透明通道)。
    anno_tile_path_fn: 可选,提供时把注记瓦片烘焙进底图瓦片(输出 PNG)。
    未提供裁剪与注记时维持原样无损复制,保持原格式(jpg/png)。

    levels: 选中的天地图级别列表。
    on_progress(done, total): 可选,细粒度上报已处理瓦片数。
    should_stop(): 可选,返回 True 时在层边界尽快停止(暂停/取消)。
    concurrency: 切片线程数;None 时取 min(CPU 核数, 8)。
    返回 (out_dir, 已导出的 gdal 级号列表, 输出扩展名, stopped)。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stopped = False
    clipping = clip_geom is not None
    annotating = anno_tile_path_fn is not None
    # 裁剪或叠加注记时需重新编码,统一 png(需透明/合成);否则用源格式无损复制
    out_ext = "png" if (clipping or annotating) else provider.ext
    geoms = clip_bounds = None
    if clipping:
        geoms = prepare_geoms(clip_geom, "EPSG:4326")   # TMS 为 4326
        clip_bounds = _bounds_of(geoms)

    # 细粒度进度:总瓦片数(所有层级之和),每处理一张回调一次
    total_tiles = max(sum(range_for_bbox(*bbox, z).count for z in levels), 1)
    workers = concurrency if concurrency and concurrency > 0 else min(os.cpu_count() or 4, 8)

    lock = threading.Lock()
    done_ref = [0]
    exported_set: set[int] = set()

    def _bump():
        with lock:
            done_ref[0] += 1
            d = done_ref[0]
        if on_progress and d % 32 == 0:
            on_progress(d, total_tiles)

    def _worker(col, row, z):
        wrote = _render_tms_tile(col, row, z, out_dir, out_ext, tile_path_fn,
                                 clipping, annotating, geoms, clip_bounds,
                                 anno_tile_path_fn)
        _bump()
        return tms_level(z) if wrote else None

    # 瓦片相互独立、无共享可变状态,直接线程池并行(GDAL/IO 释放 GIL)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for z in levels:
            if should_stop and should_stop():
                stopped = True
                break
            tr = range_for_bbox(*bbox, z)
            futures = []
            for i, (col, row) in enumerate(tr.iter_tiles()):
                # 提交过程中定期查停止:及时中断大层级,取消未开始的任务
                if should_stop and i % 256 == 0 and should_stop():
                    stopped = True
                    break
                futures.append(pool.submit(_worker, col, row, z))
            for fut in futures:
                r = fut.result()
                if r is not None:
                    exported_set.add(r)
            if stopped:
                break

    exported = sorted(exported_set)
    if on_progress and not stopped:
        on_progress(total_tiles, total_tiles)
    return out_dir, exported, out_ext, stopped


def write_tilemapresource(
    out_dir: Path,
    provider: TileProvider,
    title: str,
    bbox: tuple[float, float, float, float],
    levels: list[int],
    ext: str | None = None,
) -> Path:
    """生成 tilemapresource.xml,格式对齐示例 NaturalEarthII。

    BoundingBox 用瓦片区间对齐后的四至(最大级别),SRS=EPSG:4326,
    profile=geodetic,原点左下角。ext 指定实际瓦片扩展名(裁剪时为 png)。
    """
    # 用最大级别的瓦片对齐四至作为数据范围
    tr_max = range_for_bbox(*bbox, max(levels))
    west, south, east, north = tr_max.mosaic_bounds()

    tile_ext = ext or provider.ext
    mime = "image/jpeg" if tile_ext in ("jpg", "jpeg") else f"image/{tile_ext}"

    tilesets = []
    for z in levels:
        level = tms_level(z)
        upp = units_per_pixel(level)
        tilesets.append(
            f'        <TileSet href="{level}" units-per-pixel="{upp:.14f}" order="{level}"/>'
        )
    tilesets_xml = "\n".join(tilesets)

    xml = f"""<?xml version="1.0" encoding="utf-8"?>
    <TileMap version="1.0.0" tilemapservice="http://tms.osgeo.org/1.0.0">
      <Title>{title}</Title>
      <Abstract></Abstract>
      <SRS>EPSG:4326</SRS>
      <BoundingBox miny="{south:.14f}" minx="{west:.14f}" maxy="{north:.14f}" maxx="{east:.14f}"/>
      <Origin y="-90.00000000000000" x="-180.00000000000000"/>
      <TileFormat width="256" height="256" mime-type="{mime}" extension="{tile_ext}"/>
      <TileSets profile="geodetic">
{tilesets_xml}
      </TileSets>
    </TileMap>
"""
    path = out_dir / "tilemapresource.xml"
    path.write_text(xml, encoding="utf-8")
    return path
