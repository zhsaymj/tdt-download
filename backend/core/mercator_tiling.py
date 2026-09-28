"""EPSG:3857 Web 墨卡托 XYZ 瓦片网格数学 —— 3857 网格的唯一实现。

天地图用 EPSG:4326(core/tiling.py);本模块服务所有墨卡托 XYZ 数据源:
Esri Terrain3D DEM、Google 影像、Esri World Imagery,以及 OSM/XYZ 导出。

抽出的原因:dem_tiling 与 osm 原先各有一份完全相同的实现,再加影像数据源
就是第三份。两者现在都从这里 import,不再各自维护。

XYZ 约定:x 自西向东(0 在 -180°),y 自北向南(0 在顶部),每级 2^z × 2^z 张。
"""
from __future__ import annotations

import math

from .tiling import TileRange

TILE_SIZE = 256
# Web 墨卡托世界范围半边长(米)
MERC_MAX = 20037508.342789244
# Web 墨卡托纬度上限(度)
LAT_LIMIT = 85.05112878


def lonlat_to_xyz(lon: float, lat: float, z: int) -> tuple[int, int]:
    """经纬度 → XYZ 瓦片行列 (x, y)。"""
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


def mercator_range_for_bbox(
    west: float, south: float, east: float, north: float, z: int
) -> TileRange:
    """给定经纬度矩形范围与级别,计算覆盖的墨卡托 XYZ 瓦片区间。

    复用 TileRange(col=x, row=y),供下载器与拼接使用。
    """
    x0, y0 = lonlat_to_xyz(west, north, z)   # 左上
    x1, y1 = lonlat_to_xyz(east, south, z)   # 右下
    col_min, col_max = min(x0, x1), max(x0, x1)
    row_min, row_max = min(y0, y1), max(y0, y1)
    return TileRange(z, col_min, col_max, row_min, row_max)


def mosaic_bounds_3857(tr: TileRange) -> tuple[float, float, float, float]:
    """整个 XYZ 瓦片区间拼接后的 3857 地理四至 (minx, miny, maxx, maxy)。"""
    minx, _, _, maxy = tile_bounds_3857(tr.col_min, tr.row_min, tr.z)  # 左上瓦片
    _, miny, maxx, _ = tile_bounds_3857(tr.col_max, tr.row_max, tr.z)  # 右下瓦片
    return minx, miny, maxx, maxy


def estimate_mercator_tiles(
    bbox: tuple[float, float, float, float], levels: list[int]
) -> int:
    """按选中级别估算墨卡托瓦片总数。

    刻意不设上限、不做拦截:调用方(api 层)只负责如实呈现规模,
    是否下载由用户决定(设计 §4.10 / §9 Q1)。
    """
    w, s, e, n = bbox
    return sum(mercator_range_for_bbox(w, s, e, n, z).count for z in levels)


def _tile_xyz_range(bbox, z: int):
    """给定经纬度 bbox 与级别,返回覆盖的 (x 列表, y 列表)。

    osm.py 的切片循环用它;与 mercator_range_for_bbox 等价,
    只是返回 range 对象而非 TileRange。
    """
    west, south, east, north = bbox
    x0, y0 = lonlat_to_xyz(west, north, z)   # 左上
    x1, y1 = lonlat_to_xyz(east, south, z)   # 右下
    xs = range(min(x0, x1), max(x0, x1) + 1)
    ys = range(min(y0, y1), max(y0, y1) + 1)
    return xs, ys
