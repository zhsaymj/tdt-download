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


def crop_to_bbox(src_path: Path, bbox: tuple[float, float, float, float],
                 bbox_crs: str = "EPSG:4326") -> bool:
    """把栅格原地裁到 bbox 的外接矩形(窗口裁剪,不是几何遮罩)。

    为什么 DEM 不复用 clip_to_geometry:那个走 rasterio.mask 逐像素判几何内外,
    把外部设为 nodata——尺寸不变,只是边缘变空。而 DEM 的诉求是"成果边界落在
    选区上":拼接是按**瓦片区间**出图的(mosaic_bounds_3857),边界是瓦片网格边界,
    低层级能超出选区好几倍(实测 z=12 时东边多出 0.087°,比 0.071° 的选区还宽)。
    故这里真正裁掉多余像素、缩小尺寸,而非留着一圈 nodata。

    单波段浮点高程也不适合加 alpha 通道,边界外用 nodata 表达即可。

    bbox_crs 与栅格 CRS 不同时先换算 bbox(DEM 成果常在 EPSG:3857)。
    返回 True=已裁剪;范围无交集或已在范围内时返回 False(不动文件)。
    """
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds as window_from_bounds

    with rasterio.open(src_path) as src:
        src_crs = src.crs.to_string() if src.crs else "EPSG:4326"
        want = (transform_bounds(bbox_crs, src_crs, *bbox)
                if src_crs != bbox_crs else bbox)
        b = src.bounds
        # 交集;完全无交集说明 bbox 与数据不匹配,保持原样并交由调用方记日志
        ix0, iy0 = max(want[0], b.left), max(want[1], b.bottom)
        ix1, iy1 = min(want[2], b.right), min(want[3], b.top)
        if ix1 <= ix0 or iy1 <= iy0:
            return False
        # 已经落在 bbox 内就不必重写文件。阈值取一整个像素而非半个:窗口按整像素
        # 对齐后,边界与 bbox 必然残留最多一个像素的差,用半像素判会永远为真、
        # 每次重跑都白重写一遍文件(且浮点相等在边界上不可靠)。
        px, py = abs(src.transform.a), abs(src.transform.e)
        eps = 1e-9
        if (ix0 - b.left <= px + eps and b.right - ix1 <= px + eps
                and iy0 - b.bottom <= py + eps and b.top - iy1 <= py + eps):
            return False
        win = window_from_bounds(ix0, iy0, ix1, iy1, src.transform)
        # 对齐到整像素:非整数窗口会让 transform 带半像素偏移,坐标就不准了
        win = win.round_offsets().round_lengths()
        data = src.read(window=win)
        profile = src.profile.copy()
        profile.update(height=int(win.height), width=int(win.width),
                       transform=src.window_transform(win))

    tmp = src_path.with_suffix(".crop.tmp.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data)
    tmp.replace(src_path)
    return True


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
    """把 EPSG:4326 的 GeoTIFF 原地裁剪到 geometry 边界,边界外置黑 + 写掩膜波段。

    不用 nodata=0 表达"边界外":三波段 uint8 影像里 0 是合法像素值,深色植被的
    红波段经常正好取 0(实测 z13 有 6.5% 的有效像素至少有一个波段为 0)。一旦
    声明 nodata=0,QGIS 按波段套用 nodata,选区**内部**这些像素也被判成无数据、
    渲成白点,看着像影像丢像素。
    故改用 GDAL 掩膜波段:像素值原样保留,有效区只靠 mask 表达。下游 tms/osm
    切片本就用 read_masks 取 alpha(见 tms.py/osm.py),掩膜同样能正确传下去,
    且不会再在植被区打出透明散点。

    成功裁剪返回 True;几何为空或无重叠返回 False(原文件不动)。
    """
    from rasterio.features import geometry_mask

    geoms = _geojson_geometries(geometry)
    if not geoms:
        return False

    with rasterio.open(src_path) as src:
        try:
            # 几何外仍填 0(保持黑边):不认掩膜的老客户端看到的是黑边而非花屏
            out_image, out_transform = rio_mask(
                src, geoms, crop=True, filled=True, nodata=0
            )
        except ValueError:
            # 几何与影像无重叠
            return False
        profile = src.profile.copy()

    height, width = out_image.shape[1], out_image.shape[2]
    # invert=True -> 几何内为 True。与 rio_mask 同样用默认 all_touched=False,
    # 保证掩膜边界和上面裁出来的像素边界完全一致。
    inside = geometry_mask(geoms, out_shape=(height, width),
                           transform=out_transform, invert=True)
    valid = np.where(inside, 255, 0).astype(np.uint8)

    profile.update({
        "height": height,
        "width": width,
        "transform": out_transform,
        # 大范围裁剪结果仍可能超 4GB 普通 TIFF 上限,显式启用 BIGTIFF
        "BIGTIFF": "YES",
    })
    # 旧版成果(或上游)可能已带 nodata=0,必须显式清掉,否则白点照旧
    profile.pop("nodata", None)

    tmp = src_path.with_suffix(".clip.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(out_image)
        dst.write_mask(valid)
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
        # 裁剪过的影像用掩膜波段表达有效区(见 clip_to_geometry),重投影必须把
        # 掩膜一起搬过去,否则投影版会丢掉裁剪边界的透明信息。
        src_mask = src.read_masks(1)
        has_mask = bool(src_mask.min() < 255)

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
        if has_mask:
            dst_mask = np.zeros((height, width), dtype=np.uint8)
            reproject(
                source=src_mask,
                destination=dst_mask,
                src_transform=src_transform,
                src_crs=src_crs,
                dst_transform=transform,
                dst_crs=dst_crs,
                # 掩膜是 0/255 二值,必须最近邻,双线性会在边界糊出中间值
                resampling=Resampling.nearest,
            )
            dst.write_mask(dst_mask)
    tmp.replace(src_path)
    return True
