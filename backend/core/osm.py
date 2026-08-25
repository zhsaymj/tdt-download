"""导出 OSM(OpenStreetMap)XYZ 瓦片格式(EPSG:3857 Web 墨卡托)。

天地图瓦片是 EPSG:4326 网格,OSM/Web 墨卡托是 EPSG:3857,两者不同构,
必须重投影重采样。做法:以最高级别的 4326 拼接图为源,对每个选中级别、
每张 XYZ 瓦片单独 reproject 到该瓦片的 3857 地理范围(256x256),写出 PNG。

XYZ 约定(与示例数据 osm_tiles_tdt_jrg 一致):
  - 目录结构 {z}/{x}/{y}.png
  - x 自西向东(0 在 -180°),y 自北向南(0 在顶部),每级 2^z × 2^z 张
"""
from __future__ import annotations

import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
import rasterio.shutil as rio_shutil
from rasterio.enums import ColorInterp, Resampling
from rasterio.io import MemoryFile
from rasterio.transform import from_bounds
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds as window_from_bounds

from .tile_clip import prepare_geoms, tile_alpha_mask, tile_relation, _bounds_of

TILE_SIZE = 256
# Web 墨卡托世界范围半边长(米)
MERC_MAX = 20037508.342789244
# Web 墨卡托纬度上限(度)
LAT_LIMIT = 85.05112878


def lonlat_to_xyz(lon: float, lat: float, z: int) -> tuple[int, int]:
    """经纬度 → OSM XYZ 瓦片行列 (x, y)。"""
    lat = max(-LAT_LIMIT, min(LAT_LIMIT, lat))
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tile_bounds_3857(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """XYZ 瓦片在 EPSG:3857 下的地理范围 (minx, miny, maxx, maxy),单位米。"""
    n = 2 ** z
    span = 2 * MERC_MAX / n
    minx = -MERC_MAX + x * span
    maxx = minx + span
    maxy = MERC_MAX - y * span
    miny = maxy - span
    return minx, miny, maxx, maxy


def _tile_xyz_range(bbox, z: int):
    """给定经纬度 bbox 与级别,返回覆盖的 (x 列表, y 列表)。"""
    west, south, east, north = bbox
    x0, y0 = lonlat_to_xyz(west, north, z)   # 左上
    x1, y1 = lonlat_to_xyz(east, south, z)   # 右下
    xs = range(min(x0, x1), max(x0, x1) + 1)
    ys = range(min(y0, y1), max(y0, y1) + 1)
    return xs, ys


def _write_png(path: Path, arr: np.ndarray, transform) -> None:
    """把 (bands,256,256) uint8 数组写成 PNG(含 alpha 波段时输出透明 PNG)。

    GDAL 的 PNG 驱动只支持 CreateCopy,先写内存 GTiff 再 copy 成 PNG。
    最后一个波段作为 alpha:下载范围外(未覆盖)的像素透明,消除黑边。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = _bleed_rgb_into_transparent_pixels(arr)
    bands = arr.shape[0]
    profile = {
        "driver": "GTiff", "height": TILE_SIZE, "width": TILE_SIZE,
        "count": bands, "dtype": "uint8", "crs": "EPSG:3857", "transform": transform,
    }
    # 关闭 PAM,避免每张瓦片旁生成 .png.aux.xml(与示例数据保持一致的纯 PNG)
    with rasterio.Env(GDAL_PAM_ENABLED="NO"):
        with MemoryFile() as mem:
            with mem.open(**profile) as tmp:
                tmp.write(arr)
                # 标注最后一个波段为 alpha,PNG 驱动据此输出透明通道
                tmp.colorinterp = [
                    *tmp.colorinterp[:bands - 1], ColorInterp.alpha,
                ]
            with mem.open() as tmp:
                rio_shutil.copy(tmp, str(path), driver="PNG")


def _bleed_rgb_into_transparent_pixels(arr: np.ndarray, max_iter: int = 8) -> np.ndarray:
    """把透明像素旁边的 RGB 向外扩几圈,避免 PNG 纹理采样出现黑边。

    OSM 边缘瓦片会有 alpha=0 的空白区。PNG 虽然透明,但这些像素的 RGB
    默认是 0,部分预览器/纹理线性采样会把透明黑参与插值,视觉上出现黑线。
    只改透明像素的 RGB,不改 alpha,因此不会改变瓦片真实有效范围。
    """
    if arr.shape[0] < 4:
        return arr
    alpha = arr[-1]
    transparent = alpha == 0
    filled = alpha > 0
    if not transparent.any() or not filled.any():
        return arr

    out = arr.copy()
    rgb = out[:-1]
    h, w = alpha.shape
    directions = (
        (-1, 0), (1, 0), (0, -1), (0, 1),
        (-1, -1), (-1, 1), (1, -1), (1, 1),
    )
    for _ in range(max_iter):
        newly = np.zeros_like(filled, dtype=bool)
        for dy, dx in directions:
            src_y = slice(max(0, -dy), h - max(0, dy))
            src_x = slice(max(0, -dx), w - max(0, dx))
            dst_y = slice(max(0, dy), h - max(0, -dy))
            dst_x = slice(max(0, dx), w - max(0, -dx))
            candidates = transparent[dst_y, dst_x] & ~filled[dst_y, dst_x] & filled[src_y, src_x]
            if not candidates.any():
                continue
            for b in range(rgb.shape[0]):
                dst_band = rgb[b, dst_y, dst_x]
                src_band = rgb[b, src_y, src_x]
                dst_band[candidates] = src_band[candidates]
            newly_view = newly[dst_y, dst_x]
            newly_view[candidates] = True
        if not newly.any():
            break
        filled |= newly
    return out


def _render_one_tile(vrt, vrt_bounds, bands, z, x, y, dst_png,
                     clip_geoms_3857) -> bool:
    """渲染并写出单张 OSM 瓦片。返回 True=已写出,False=跳过(无覆盖)。

    仅用传入的 vrt(须为调用线程自己的 WarpedVRT,不跨线程共享)。
    逻辑与单线程版一致:窗口读交集区 → 贴 256 画布 → alpha/裁剪遮罩 → 写 PNG。
    """
    minx, miny, maxx, maxy = tile_bounds_3857(x, y, z)
    rgb = np.zeros((bands, TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    alpha = np.zeros((TILE_SIZE, TILE_SIZE), dtype=np.uint8)
    ix0, iy0 = max(minx, vrt_bounds.left), max(miny, vrt_bounds.bottom)
    ix1, iy1 = min(maxx, vrt_bounds.right), min(maxy, vrt_bounds.top)
    if ix1 <= ix0 or iy1 <= iy0:
        return False
    span = maxx - minx
    px0 = int(round((ix0 - minx) / span * TILE_SIZE))
    px1 = int(round((ix1 - minx) / span * TILE_SIZE))
    py0 = int(round((maxy - iy1) / span * TILE_SIZE))
    py1 = int(round((maxy - iy0) / span * TILE_SIZE))
    ow, oh = px1 - px0, py1 - py0
    if ow <= 0 or oh <= 0:
        return False
    window = window_from_bounds(ix0, iy0, ix1, iy1, vrt.transform)
    sub = vrt.read(out_shape=(bands, oh, ow), window=window,
                   resampling=Resampling.bilinear).astype(np.uint8)
    submask = vrt.read_masks(1, out_shape=(oh, ow), window=window).astype(np.uint8)
    rgb[:, py0:py1, px0:px1] = sub
    alpha[py0:py1, px0:px1] = submask
    if clip_geoms_3857 is not None:
        gmask = tile_alpha_mask((minx, miny, maxx, maxy), clip_geoms_3857)
        alpha = np.minimum(alpha, gmask)
    if not alpha.any():
        return False
    dst_transform = from_bounds(minx, miny, maxx, maxy, TILE_SIZE, TILE_SIZE)
    rgba = np.concatenate([rgb, alpha[np.newaxis, :, :]], axis=0)
    _write_png(dst_png, rgba, dst_transform)
    return True


def export_osm(
    src_geotiff: Path,
    levels: list[int],
    bbox: tuple[float, float, float, float],
    out_dir: Path,
    on_progress=None,
    clip_geom: dict | None = None,
    should_stop=None,
    concurrency: int | None = None,
) -> tuple[Path, list[int], bool]:
    """把 EPSG:4326 的源 GeoTIFF 重投影切成 OSM XYZ 瓦片(多线程)。

    src_geotiff: 最高级别的 4326 拼接图(分辨率最高,作为重采样源)。
    on_progress(done_tiles, total_tiles): 可选回调,细粒度上报已处理瓦片数。
    clip_geom: 可选 geojson 几何(WGS84),提供时按其边界裁剪(边界外透明)。
    should_stop(): 可选,返回 True 时在层边界尽快停止(暂停/取消)。
    concurrency: 切片线程数;None 时取 min(CPU 核数, 8)。
    返回 (out_dir, 已导出的级别列表, stopped)。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stopped = False
    # 细粒度进度:总瓦片数(各级 xs*ys 之和,含将被跳过的空瓦片,作为进度分母)
    total_tiles = 0
    for z in levels:
        xs, ys = _tile_xyz_range(bbox, z)
        total_tiles += len(list(xs)) * len(list(ys))
    total_tiles = max(total_tiles, 1)
    done_tiles = 0

    # 裁剪几何:转到 3857(与 OSM 瓦片同坐标系),预备逐瓦片遮罩
    clip_geoms_3857 = None
    clip_bounds = None
    if clip_geom:
        clip_geoms_3857 = prepare_geoms(clip_geom, "EPSG:3857")
        clip_bounds = _bounds_of(clip_geoms_3857)

    # 用 WarpedVRT 把源包装成"虚拟的 EPSG:3857 数据集":重投影延迟到读取时按需做,
    # 不一次性读全图;每张瓦片只按其范围窗口读 + 重采样到 256,低层级自动走 overview。
    # 多线程:瓦片相互独立,GDAL 在重投影/IO 时释放 GIL,故用线程池即可真正并行。
    # rasterio dataset/WarpedVRT 非线程安全,每个线程用 threading.local 各建各的 vrt。
    workers = concurrency if concurrency and concurrency > 0 else min(os.cpu_count() or 4, 8)

    # 先取源的 3857 有效四至(用于覆盖判断);顺便拿波段数
    with rasterio.open(src_geotiff) as _ds0:
        bands = _ds0.count
        with WarpedVRT(_ds0, crs="EPSG:3857", resampling=Resampling.bilinear,
                       src_nodata=_ds0.nodata, nodata=0) as _v0:
            vb = _v0.bounds
    vrt_left, vrt_bottom, vrt_right, vrt_top = vb.left, vb.bottom, vb.right, vb.top

    _tls = threading.local()
    _open_handles: list[tuple] = []   # (ds, vrt) 注册表,结束后统一关闭
    lock = threading.Lock()

    def _get_vrt():
        """取当前线程专属的 (ds, vrt);首次调用时创建并登记以便收尾关闭。"""
        v = getattr(_tls, "vrt", None)
        if v is None:
            ds = rasterio.open(src_geotiff)
            v = WarpedVRT(ds, crs="EPSG:3857", resampling=Resampling.bilinear,
                          src_nodata=ds.nodata, nodata=0)
            _tls.ds = ds
            _tls.vrt = v
            with lock:
                _open_handles.append((ds, v))
        return _tls.vrt

    done_ref = [0]
    exported_set: set[int] = set()

    def _bump_progress():
        with lock:
            done_ref[0] += 1
            d = done_ref[0]
        if on_progress and d % 16 == 0:
            on_progress(d, total_tiles)

    def _worker(z, x, y, dst_png):
        """线程任务:渲染单瓦片。返回是否写出(用于收集 exported 级别)。"""
        vrt = _get_vrt()
        wrote = _render_one_tile(vrt, vb, bands, z, x, y, dst_png, clip_geoms_3857)
        _bump_progress()
        return z if wrote else None

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for z in levels:
                if should_stop and should_stop():
                    stopped = True
                    break
                xs, ys = _tile_xyz_range(bbox, z)
                futures = []
                for x in xs:
                    if should_stop and should_stop():
                        stopped = True
                        break
                    for y in ys:
                        dst_png = out_dir / str(z) / str(x) / f"{y}.png"
                        # 断点续切:已存在且非空直接跳过(不占线程、只算进度)
                        if dst_png.exists() and dst_png.stat().st_size > 0:
                            exported_set.add(z)
                            _bump_progress()
                            continue
                        minx, miny, maxx, maxy = tile_bounds_3857(x, y, z)
                        # 完全在源覆盖范围外:无数据,跳过(只算进度)
                        if (maxx <= vrt_left or minx >= vrt_right
                                or maxy <= vrt_bottom or miny >= vrt_top):
                            _bump_progress()
                            continue
                        # 裁剪:完全在几何外接框外的瓦片直接跳过(只算进度)
                        if clip_geoms_3857 is not None and \
                                tile_relation((minx, miny, maxx, maxy),
                                              clip_bounds) == "outside":
                            _bump_progress()
                            continue
                        futures.append(pool.submit(_worker, z, x, y, dst_png))
                # 收集本级结果(等本级切完再进下一级,便于按级 exported/停止判断)
                for fut in futures:
                    r = fut.result()
                    if r is not None:
                        exported_set.add(r)
                if stopped:
                    break
    finally:
        # 关闭各线程创建的 vrt/ds,避免文件句柄泄漏(服务长跑会累积)
        for ds, v in _open_handles:
            try:
                v.close(); ds.close()
            except Exception:
                pass

    exported = sorted(exported_set)
    if on_progress and not stopped:
        on_progress(total_tiles, total_tiles)
    return out_dir, exported, stopped
