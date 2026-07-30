"""GeoTIFF 后处理:按矢量几何裁剪 + 重投影到目标坐标系。

均基于 rasterio 自带能力(mask / warp),无需额外依赖。
处理顺序:先在 EPSG:4326 下按几何裁剪(几何本身是 WGS84,直接匹配),
再按需重投影到目标 CRS。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.mask import mask as rio_mask
from rasterio.warp import calculate_default_transform, reproject, Resampling


def _geojson_geometries(geometry: dict) -> list[dict]:
    """把 geojson 几何/要素/集合展开成几何 dict 列表(供 rasterio.mask 用)。"""
    if not geometry:
        return []
    t = geometry.get("type")
    if t == "FeatureCollection":
        return [f["geometry"] for f in geometry.get("features", []) if f.get("geometry")]
    if t == "Feature":
        return [geometry["geometry"]] if geometry.get("geometry") else []
    if t == "GeometryCollection":
        return list(geometry.get("geometries", []))
    # 直接就是几何
    return [geometry]


def clip_to_geometry(src_path: Path, geometry: dict) -> bool:
    """把 EPSG:4326 的 GeoTIFF 原地裁剪到 geometry 边界,边界外设为 nodata。

    成功裁剪返回 True;几何为空或无重叠返回 False(原文件不动)。
    """
    geoms = _geojson_geometries(geometry)
    if not geoms:
        return False

    with rasterio.open(src_path) as src:
        try:
            out_image, out_transform = rio_mask(
                src, geoms, crop=True, filled=True, nodata=0
            )
        except ValueError:
            # 几何与影像无重叠
            return False
        profile = src.profile.copy()

    profile.update({
        "height": out_image.shape[1],
        "width": out_image.shape[2],
        "transform": out_transform,
        "nodata": 0,
        # 大范围裁剪结果仍可能超 4GB 普通 TIFF 上限,显式启用 BIGTIFF
        "BIGTIFF": "YES",
    })

    tmp = src_path.with_suffix(".clip.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(out_image)
    tmp.replace(src_path)
    return True


def reproject_geotiff(src_path: Path, dst_crs: str) -> bool:
    """把 GeoTIFF 原地重投影到 dst_crs(如 EPSG:4547)。

    dst_crs 与源相同或为空则跳过,返回 False。
    """
    if not dst_crs:
        return False
    with rasterio.open(src_path) as src:
        if src.crs and src.crs.to_string() == dst_crs:
            return False
        transform, width, height = calculate_default_transform(
            src.crs, dst_crs, src.width, src.height, *src.bounds
        )
        profile = src.profile.copy()
        profile.update({
            "crs": dst_crs,
            "transform": transform,
            "width": width,
            "height": height,
            # 大范围高程/影像重投影后可能超 4GB 普通 TIFF 上限(32位偏移会写坏文件
            # 并抛 TIFFAppendToStrip:Maximum TIFF file size exceeded)。用 BIGTIFF
            # 突破,GDAL 据实际大小写 64 位偏移,小文件不受影响。
            "BIGTIFF": "YES",
        })
        src_data = [src.read(i) for i in range(1, src.count + 1)]
        src_crs = src.crs
        src_transform = src.transform
        src_nodata = src.nodata

    tmp = src_path.with_suffix(".warp.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        for i, band in enumerate(src_data, start=1):
            reproject(
                source=band,
                destination=rasterio.band(dst, i),
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                src_nodata=src_nodata,
                dst_nodata=src_nodata,
                resampling=Resampling.bilinear,
            )
    tmp.replace(src_path)
    return True
