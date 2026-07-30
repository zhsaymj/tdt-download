"""DEM(EPSG:3857 Web 墨卡托 XYZ)瓦片行列换算。

天地图用 EPSG:4326(core/tiling.py),Esri Terrain3D DEM 用 Web 墨卡托 XYZ
(与 core/osm.py 的导出网格同构)。这里把经纬度 bbox 换算成某级别覆盖的
XYZ 瓦片区间,复用 TileRange 数据结构,让现有下载器无缝下载 DEM 瓦片。

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

    复用 TileRange(col=x, row=y),供下载器与 DEM 拼接使用。
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


def estimate_dem_tiles(
    bbox: tuple[float, float, float, float], levels: list[int]
) -> int:
    """按选中级别估算 DEM 墨卡托瓦片总数。"""
    w, s, e, n = bbox
    return sum(mercator_range_for_bbox(w, s, e, n, z).count for z in levels)
