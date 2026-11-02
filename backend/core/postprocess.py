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
from rasterio.vrt import WarpedVRT
from rasterio.warp import calculate_default_transform, Resampling
from rasterio.windows import Window


# 分块流式重投影的窗口边长(像素)。2048² 的 float32 波段约 16MB,多波段
# uint8 也才 16MB —— 峰值内存与整幅尺寸无关,只与这个常数有关。
_CHUNK_PX = 2048
# WarpedVRT 内部 warp 缓存上限(MB),同样与数据规模无关。
_WARP_MEM_LIMIT_MB = 256


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
    """把 GeoTIFF 原地裁剪到 geometry 边界并写显式 alpha 波段。

    geometry 恒为 WGS84 GeoJSON(前端送来的就是这个)。源图坐标系可以是
    EPSG:4326(天地图)或 EPSG:3857(Google/Esri 影像)—— 后者必须先把几何
    转到源图坐标系,否则经纬度(±180)与米(±2e7)不在一个尺度上,rio_mask
    会判成"无重叠"、**静默返回 False 而图一点没裁**。
    做法与 core/tile_clip.py 的 prepare_geoms 一致。

    不用 nodata=0 表达"边界外":三波段 uint8 影像里 0 是合法像素值,深色植被的
    红波段经常正好取 0(实测 z13 有 6.5% 的有效像素至少有一个波段为 0)。一旦
    声明 nodata=0,QGIS 按波段套用 nodata,选区**内部**这些像素也被判成无数据、
    渲成白点,看着像影像丢像素。
    因此输出保留颜色像素值,并把几何范围与源图有效性合并到显式 alpha。这样
    桌面查看器和下游 tms/osm 切片都能识别透明区,且不会在植被区打出透明散点。

    成功裁剪返回 True;几何为空或无重叠返回 False(原文件不动)。
    """
    from rasterio.features import geometry_mask

    geoms = _geojson_geometries(geometry)
    if not geoms:
        return False

    # 几何是 WGS84;源图可能是 3857。必须先转到源图坐标系 —— 否则
    # rio_mask 判成无重叠,**静默返回 False**(图没裁,也没有任何报错)。
    with rasterio.open(src_path) as _probe:
        _src_crs = _probe.crs
    if _src_crs is not None and _src_crs.to_string() != "EPSG:4326":
        from rasterio.warp import transform_geom
        geoms = [transform_geom("EPSG:4326", _src_crs.to_string(), g)
                 for g in geoms]

    with rasterio.open(src_path) as src:
        try:
            # 几何外填 0,同时把有效性写入显式 alpha,避免不认内部 mask 的客户端显示黑边
            masked_image, out_transform = rio_mask(
                src, geoms, crop=True, filled=False
            )
        except ValueError:
            # 几何与影像无重叠
            return False
        profile = src.profile.copy()
        source_colorinterp = tuple(src.colorinterp)

    out_image = np.ma.getdata(masked_image)
    masked = np.ma.getmaskarray(masked_image)
    if masked.ndim == 2:
        masked = masked[np.newaxis, ...]
    height, width = out_image.shape[1], out_image.shape[2]
    # invert=True -> 几何内为 True。与 rio_mask 同样用默认 all_touched=False,
    # 保证掩膜边界和上面裁出来的像素边界完全一致。
    inside = geometry_mask(geoms, out_shape=(height, width),
                           transform=out_transform, invert=True)
    alpha_indexes = [
        i for i, ci in enumerate(source_colorinterp)
        if ci == rasterio.enums.ColorInterp.alpha
    ]
    data_indexes = [
        i for i in range(out_image.shape[0]) if i not in alpha_indexes
    ] or [0]
    source_valid = ~masked[data_indexes].any(axis=0)
    if alpha_indexes:
        source_alpha = np.asarray(out_image[alpha_indexes[0]], dtype=np.uint8)
        valid = np.where(inside & source_valid, source_alpha, 0).astype(np.uint8)
    else:
        valid = np.where(inside & source_valid, 255, 0).astype(np.uint8)
    data = np.asarray(out_image[data_indexes], dtype=out_image.dtype).copy()
    data[:, valid == 0] = 0

    profile.update({
        "height": height,
        "width": width,
        "transform": out_transform,
        "count": len(data_indexes) + 1,
        "photometric": "RGB" if len(data_indexes) >= 3 else "MINISBLACK",
        # 大范围裁剪结果仍可能超 4GB 普通 TIFF 上限,显式启用 BIGTIFF
        "BIGTIFF": "YES",
    })
    # 旧版成果(或上游)可能已带 nodata=0,必须显式清掉,否则白点照旧
    profile.pop("nodata", None)

    tmp = src_path.with_suffix(".clip.tif")
    with rasterio.open(tmp, "w", **profile) as dst:
        dst.write(data, indexes=list(range(1, len(data_indexes) + 1)))
        dst.write(valid, len(data_indexes) + 1)
        if len(data_indexes) >= 3:
            dst.colorinterp = [
                rasterio.enums.ColorInterp.red,
                rasterio.enums.ColorInterp.green,
                rasterio.enums.ColorInterp.blue,
                rasterio.enums.ColorInterp.alpha,
            ]
        else:
            dst.colorinterp = [
                rasterio.enums.ColorInterp.gray,
                rasterio.enums.ColorInterp.alpha,
            ]
    tmp.replace(src_path)
    return True


def _chunk_windows(width: int, height: int, chunk: int | None = None):
    """把目标栅格切成 chunk 见方的写窗口(流式重投影用)。"""
    if chunk is None:
        chunk = _CHUNK_PX
    for row_off in range(0, height, chunk):
        h = min(chunk, height - row_off)
        for col_off in range(0, width, chunk):
            w = min(chunk, width - col_off)
            yield Window(col_off, row_off, w, h)


def _src_has_mask(src) -> bool:
    """源栅格是否存在非全有效的掩膜(按窗口扫描,不整幅读入)。

    无 nodata 且无 alpha 波段时 GDAL 的掩膜恒为 255,直接短路 —— 这是最常见
    的影像路径,省掉一次全图扫描。其余情况逐窗口探测,遇到第一个 0 立即返回。
    """
    if src.nodata is None and not any(
            ci == rasterio.enums.ColorInterp.alpha for ci in src.colorinterp):
        return False
    for window in _chunk_windows(src.width, src.height):
        if src.read_masks(1, window=window).min() < 255:
            return True
    return False


def reproject_geotiff(src_path: Path, dst_crs: str,
                      resampling: Resampling = Resampling.bilinear,
                      on_progress=None) -> bool:
    """把 GeoTIFF 原地重投影到 dst_crs(如 EPSG:4547)。

    dst_crs 与源相同或为空则跳过,返回 False。
    on_progress(done_windows, total_windows): 可选,每写完一个分块窗口回调
    一次——本函数已是分块流式(见下方说明),加这个回调只是把现成的循环进度
    暴露出去,不改变内部处理方式。大范围/高分辨率栅格单次重投影可能耗时
    数十秒,没有回调时调用方的进度条会在这期间停滞不动,容易被误认为卡死。

    resampling 只作用于**彩色波段**;alpha 与掩膜恒用 nearest(它们是有效性
    边界,插值会把透明区扩成灰边)。默认 bilinear 是既有行为,不要随手改 ——
    用户选的"输出坐标系"走的就是这条。TMS 的 4326 源另传 cubic,理由见
    runner._mercator_raster_source。

    **必须分块流式**:早期实现用 [src.read(i) ...] 把整幅源一次性读进内存,
    再整幅重投影。全国 z13 高程图实测 46336x29440 float32 = 5.08GB,目标 4326
    再要 4.55GB,峰值直接撞穿 Windows 提交上限,报错表现为
    "cannot allocate 262144 bytes"(CPLE_OutOfMemoryError,只是一个 256KB 的
    块分配失败)—— 掩盖了真正的整幅数组超额。现在改为 WarpedVRT + 分块窗口
    读写,峰值内存恒为 _CHUNK_PX 量级,与栅格尺寸无关。
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
        src_nodata = src.nodata
        n_bands = src.count
        alpha_indexes = {
            i for i, ci in enumerate(src.colorinterp, start=1)
            if ci == rasterio.enums.ColorInterp.alpha
        }
        # 裁剪过的影像用掩膜波段表达有效区(见 clip_to_geometry),重投影必须把
        # 掩膜一起搬过去,否则投影版会丢掉裁剪边界的透明信息。
        has_mask = _src_has_mask(src)

        # 提前算总窗口数供 on_progress 用;chunk 大小与 _chunk_windows 默认一致
        total_windows = (len(range(0, height, _CHUNK_PX))
                         * len(range(0, width, _CHUNK_PX)))
        done_windows = 0

        tmp = src_path.with_suffix(".warp.tif")
        with rasterio.open(tmp, "w", **profile) as dst:
            vrt_kwargs = dict(
                crs=dst_crs, transform=transform, width=width, height=height,
                src_nodata=src_nodata, nodata=src_nodata,
                # warp 内部缓存给固定上限,不随数据规模增长
                warp_mem_limit=_WARP_MEM_LIMIT_MB,
            )
            with WarpedVRT(src, resampling=resampling, **vrt_kwargs) as vrt:
                # alpha 是有效性边界,必须最近邻:插值会把透明边界扩成灰边。
                # 单独开一个 nearest 的 VRT 读 alpha 波段(整体 resampling 只
                # 能有一个),避免把整幅源读进内存。
                vrt_alpha = (WarpedVRT(src, resampling=Resampling.nearest,
                                       **vrt_kwargs)
                             if alpha_indexes else None)
                try:
                    for window in _chunk_windows(width, height):
                        data = vrt.read(window=window)
                        if vrt_alpha is not None:
                            alpha_data = vrt_alpha.read(window=window)
                            for i in alpha_indexes:
                                data[i - 1] = alpha_data[i - 1]
                        dst.write(data, window=window)
                        if has_mask and not alpha_indexes:
                            # 掩膜是 0/255 二值,必须最近邻,双线性会在边界糊出中间值
                            dst.write_mask(vrt.read_masks(1, window=window),
                                           window=window)
                        done_windows += 1
                        if on_progress:
                            on_progress(done_windows, total_windows)
                finally:
                    if vrt_alpha is not None:
                        vrt_alpha.close()
            if alpha_indexes:
                if n_bands >= 4:
                    dst.colorinterp = [
                        rasterio.enums.ColorInterp.red,
                        rasterio.enums.ColorInterp.green,
                        rasterio.enums.ColorInterp.blue,
                        rasterio.enums.ColorInterp.alpha,
                    ]
                elif n_bands == 2:
                    dst.colorinterp = [
                        rasterio.enums.ColorInterp.gray,
                        rasterio.enums.ColorInterp.alpha,
                    ]
    tmp.replace(src_path)
    return True
