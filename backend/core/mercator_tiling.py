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


def suggest_levels_mercator(
    bbox: tuple[float, float, float, float],
    z_max_cap: int,
    z_floor: int,
    min_useful_ratio: float,
    tile_budget: int,
    depth: int,
) -> dict:
    """墨卡托网格的建议级别判据(DEM 与影像共用的内核)。

    判据同 tiling.suggest_levels:瓦片是固定网格,低级别单张就能盖住远超选区
    的范围(实测 0.07° 选区在天地图第 7 级只有 0.1% 有效占比),全选会下一堆
    几乎全是选区外内容的图。这里在 3857 下算面积占比。

    调用方传各自的参数,不要在这里塞默认值 —— DEM 与影像的取舍不同
    (见两个包装函数的说明),混在一起会让两套取舍互相污染。
    """
    from rasterio.warp import transform_bounds

    w, s, e, n = bbox
    # 选区面积在 3857 下算,与瓦片覆盖面积同坐标系才可比
    sw, ss, se, sn = transform_bounds("EPSG:4326", "EPSG:3857", w, s, e, n)
    sel_area = max((se - sw) * (sn - ss), 1e-9)

    rows = []
    for z in range(z_floor, z_max_cap + 1):
        tr = mercator_range_for_bbox(w, s, e, n, z)
        bw, bs, be, bn = mosaic_bounds_3857(tr)
        cov = max((be - bw) * (bn - bs), 1e-9)
        ratio = min(sel_area / cov, 1.0)
        rows.append({"z": z, "tiles": tr.count, "ratio": round(ratio, 4),
                     "useful": ratio >= min_useful_ratio})

    tiles_of = {r["z"]: r["tiles"] for r in rows}
    useful = [r["z"] for r in rows if r["useful"]]
    pool = useful or [r["z"] for r in rows]

    top = pool[0]
    for z in pool:
        if tiles_of[z] <= tile_budget:
            top = z
        else:
            break
    budget_limited = top < pool[-1]

    picked: list[int] = []
    total = 0
    for z in range(top, pool[0] - 1, -1):
        if z not in tiles_of:
            break
        t = tiles_of[z]
        if picked and (total + t > tile_budget or len(picked) >= depth):
            break
        picked.append(z)
        total += t
    picked.reverse()

    return {
        "levels": rows,
        "recommended": picked,
        "recommended_tiles": total,
        "budget_limited": budget_limited,
        "max_useful": useful[-1] if useful else z_max_cap,
        "min_useful": useful[0] if useful else picked[0],
    }


def suggest_mercator_levels(
    bbox: tuple[float, float, float, float],
    z_max_cap: int = 21,
    min_useful_ratio: float = 0.25,
    tile_budget: int = 8000,
    depth: int = 3,
) -> dict:
    """墨卡托**影像**的建议级别(Google / Esri World Imagery)。

    与 DEM 版(suggest_dem_levels)的两处差别,都是实测后定的:

      - **级别从 1 起而非 0**:z0 单张瓦片盖全球,对影像毫无意义。
        天地图影像的下限也是 1(api/tasks.py 的 z_floor)。
      - **预算 8000 而非 2000**:2000 是为 LERC 瓦片解码慢而定的
        (见 suggest_dem_levels 的说明);影像瓦片是 jpg,解码快得多,
        沿用 2000 会把推荐级别压得过低。

    ⚠️ tile_budget=8000 是推断值,尚未按实际拼接耗时校准(设计 §9 Q2)。

    这一道是取消瓦片数硬上限后最重要的护栏:默认值合理地低,用户
    就不会无意间触发 TB 级任务(设计 §4.10)。
    """
    return suggest_levels_mercator(bbox, z_max_cap, 1, min_useful_ratio,
                                   tile_budget, depth)
