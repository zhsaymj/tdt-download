"""瓦片按矢量几何裁剪的通用工具。

给定一张瓦片的地理范围与目标几何(同一坐标系),生成 256×256 的
alpha 遮罩:几何内部不透明(255),外部透明(0)。用于 TMS/OSM 瓦片
按下载范围的实际多边形/MultiPolygon 裁剪(边界外透明)。
"""
from __future__ import annotations

import numpy as np
from rasterio.features import geometry_mask
from rasterio.transform import from_bounds
from rasterio.warp import transform_geom

TILE_SIZE = 256


def _geoms(geometry: dict) -> list[dict]:
    """把 geojson 几何/要素/集合展开成几何 dict 列表。"""
    if not geometry:
        return []
    t = geometry.get("type")
    if t == "FeatureCollection":
        return [f["geometry"] for f in geometry.get("features", []) if f.get("geometry")]
    if t == "Feature":
        return [geometry["geometry"]] if geometry.get("geometry") else []
    if t == "GeometryCollection":
        return list(geometry.get("geometries", []))
    return [geometry]


def prepare_geoms(geometry: dict, dst_crs: str | None = None) -> list[dict]:
    """展开几何列表;dst_crs 非 4326 时把几何从 EPSG:4326 重投影过去。

    OSM 瓦片是 EPSG:3857,需先把 4326 几何转成 3857 再逐瓦片遮罩。
    """
    geoms = _geoms(geometry)
    if dst_crs and dst_crs not in ("EPSG:4326", "epsg:4326"):
        geoms = [transform_geom("EPSG:4326", dst_crs, g) for g in geoms]
    return geoms


def _bounds_of(geoms: list[dict]) -> tuple[float, float, float, float]:
    """几何列表的整体外接范围 (minx, miny, maxx, maxy)。"""
    xs_min = ys_min = float("inf")
    xs_max = ys_max = float("-inf")

    def walk(coords):
        nonlocal xs_min, ys_min, xs_max, ys_max
        if coords and isinstance(coords[0], (int, float)):
            x, y = coords[0], coords[1]
            xs_min = min(xs_min, x); xs_max = max(xs_max, x)
            ys_min = min(ys_min, y); ys_max = max(ys_max, y)
        else:
            for c in coords:
                walk(c)

    for g in geoms:
        walk(g.get("coordinates", []))
    return xs_min, ys_min, xs_max, ys_max


def tile_relation(tile_bounds, geom_bounds) -> str:
    """瓦片与几何外接框的粗略关系:'outside' | 'maybe_inside' | 'intersect'。

    - outside: 瓦片与几何外接框不相交 → 可跳过(不导出)
    - 其余情况返回 'intersect',交由像素级遮罩精确处理
    (完全包含的精确判断成本高,统一走遮罩也正确,故只快速排除完全在外的)
    """
    tminx, tminy, tmaxx, tmaxy = tile_bounds
    gminx, gminy, gmaxx, gmaxy = geom_bounds
    if tmaxx <= gminx or tminx >= gmaxx or tmaxy <= gminy or tminy >= gmaxy:
        return "outside"
    return "intersect"


def tile_alpha_mask(
    tile_bounds: tuple[float, float, float, float],
    geoms: list[dict],
) -> np.ndarray:
    """生成瓦片的 alpha 遮罩 (256,256) uint8:几何内 255,几何外 0。

    tile_bounds: (minx, miny, maxx, maxy),与 geoms 同坐标系。
    """
    minx, miny, maxx, maxy = tile_bounds
    transform = from_bounds(minx, miny, maxx, maxy, TILE_SIZE, TILE_SIZE)
    # geometry_mask: 几何内部为 False(不掩盖),外部 True
    outside = geometry_mask(
        geoms, out_shape=(TILE_SIZE, TILE_SIZE),
        transform=transform, all_touched=True, invert=False,
    )
    alpha = np.where(outside, 0, 255).astype(np.uint8)
    return alpha
