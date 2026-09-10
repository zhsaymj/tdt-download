"""DEM 瓦片解码 + 高程 GeoTIFF 拼接。

Esri Terrain3D 瓦片体是 LERC(v1)压缩的 F32 高程栅格(单位:米),
用 Esri 官方 lerc 库解码为 numpy 数组。

流程:把缓存中已下载的 LERC 瓦片解码为 float32 高程,按墨卡托瓦片区间
拼成单波段 EPSG:3857 GeoTIFF(缺失瓦片记为 nodata),供 GIS 直接做
等高线/坡度/剖面分析,后续可再重投影到目标 CRS。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_bounds
from rasterio.windows import Window

from .dem_tiling import TILE_SIZE, mosaic_bounds_3857
from .tiling import TileRange

# 缺失/无效高程的填充值(远低于地球任何真实海拔,GIS 里设为 nodata)
DEM_NODATA = -32768.0


def _check_lerc() -> None:
    """校验 LERC 解码库可用,不可用时抛出明确错误。

    lerc 是带独立 Lerc.dll 的 ctypes 包,打包(PyInstaller)时若未把包目录连同
    dll 一起收集,import 会失败。此处显式抛错,避免解码函数里被 try/except 吞掉后
    把每张瓦片当"无数据",拼出一张打不开的空高程 GeoTIFF。
    """
    try:
        import lerc  # noqa: F401
    except Exception as e:  # ImportError 或 dll 加载失败
        raise RuntimeError(
            "LERC 解码库(lerc)不可用,无法解码地形高程瓦片。"
            "若为打包版本,请确认 lerc 包及其 Lerc.dll 已随程序打包。"
            f"原始错误:{e}"
        ) from e


# Esri 在超出某区域最高 LOD 时返回一张“空瓦片”(约 67 字节、解码全 0、
# 无有效像素),而非 404。低于此字节数的瓦片一律视为无数据。
_EMPTY_LERC_MAXBYTES = 200


def is_empty_lerc(raw: bytes) -> bool:
    """判断 LERC 字节是否为 Esri 的空瓦片(超出该区域最高级别时返回)。

    双重判据:字节数极小;或 blobInfo 报告 0 个有效像素(min>max 哨兵)。
    """
    if not raw or len(raw) <= _EMPTY_LERC_MAXBYTES:
        return True
    try:
        import lerc
        info = lerc.getLercBlobInfo(raw)
        # getLercBlobInfo 返回元组,含 nValidPixels 与 zMin/zMax;
        # 空瓦片 zMin(+3.4e38) > zMax(-3.4e38)。取末两位比较更稳。
        zmin, zmax = float(info[-3]), float(info[-2])
        if zmin > zmax:
            return True
    except Exception:
        pass
    return False


def decode_lerc(path: Path) -> np.ndarray | None:
    """把一张 LERC 瓦片解码为 float32 高程数组;失败/空瓦片/无数据返回 None。

    Esri 瓦片像素尺寸可能为 256 或 257(含边缘重叠行列),这里裁到左上
    TILE_SIZE×TILE_SIZE 与墨卡托瓦片网格对齐。
    lerc.decode 返回 (retcode, ndarray, mask)。
    """
    if not (path.exists() and path.stat().st_size > 0):
        return None
    try:
        raw = path.read_bytes()
    except Exception:
        return None
    # 空瓦片(超出该区域最高 LOD)→ 当作无数据,避免拼出全黑
    if is_empty_lerc(raw):
        return None
    try:
        import lerc
        result = lerc.decode(raw)
    except Exception:
        return None
    # 从返回中挑出 ndarray(兼容不同 lerc 版本的返回形态)
    arr = None
    if isinstance(result, tuple):
        for x in result:
            if isinstance(x, np.ndarray) and x.dtype != np.dtype("uint8"):
                arr = x
                break
        if arr is None:  # 退而取任意 ndarray
            arr = next((x for x in result if isinstance(x, np.ndarray)), None)
    elif isinstance(result, np.ndarray):
        arr = result
    if arr is None:
        return None
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim == 3:
        arr = arr[0]
    if arr.ndim != 2:
        return None
    # 裁/补齐到 TILE_SIZE×TILE_SIZE(Esri 257 边缘行列丢弃)
    return arr[:TILE_SIZE, :TILE_SIZE]


def mosaic_dem_geotiff(
    tr: TileRange,
    out_path: Path,
    tile_path_fn,
    build_overviews: bool = True,
    on_row=None,
) -> Path:
    """把墨卡托 XYZ 区间的 LERC 瓦片解码拼接为单波段高程 GeoTIFF(EPSG:3857)。

    tile_path_fn(x, y, z) -> Path:复用下载器缓存路径(col=x, row=y)。
    on_row(done_rows, total_rows): 可选,每写完一"瓦片行"回调一次。
    缺失瓦片填 DEM_NODATA。
    """
    # 拼接前先确认解码库可用:不可用则明确报错,不生成空文件
    _check_lerc()

    width = tr.cols * TILE_SIZE
    height = tr.rows * TILE_SIZE

    minx, miny, maxx, maxy = mosaic_bounds_3857(tr)
    transform = from_bounds(minx, miny, maxx, maxy, width, height)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": 1,
        "dtype": "float32",
        "crs": "EPSG:3857",
        "transform": transform,
        "nodata": DEM_NODATA,
        "compress": "LZW",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        # 大范围高级别高程拼图 float32(每像素4字节)更易超 4GB 普通 TIFF 上限
        "BIGTIFF": "YES",
    }

    # 逐"瓦片行"分块写入,避免整幅 float32 canvas 占用几十 GB 内存
    # (与 mosaic.py 同策略;缺失瓦片保持 DEM_NODATA 填充)。
    total_rows = tr.rows
    decoded = 0
    with rasterio.open(out_path, "w", **profile) as dst:
        for ri, y in enumerate(range(tr.row_min, tr.row_max + 1), start=1):
            row_buf = np.full((TILE_SIZE, width), DEM_NODATA, dtype=np.float32)
            for x in range(tr.col_min, tr.col_max + 1):
                elev = decode_lerc(tile_path_fn(x, y, tr.z))
                if elev is None:
                    continue
                decoded += 1
                h, w = elev.shape
                h = min(h, TILE_SIZE)
                w = min(w, TILE_SIZE)
                px = (x - tr.col_min) * TILE_SIZE
                row_buf[:h, px:px + w] = elev[:h, :w]
            oy = (y - tr.row_min) * TILE_SIZE
            dst.write(row_buf, 1, window=Window(0, oy, width, TILE_SIZE))
            if on_row:
                on_row(ri, total_rows)
        if build_overviews:
            dst.build_overviews([2, 4, 8, 16], Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")

    # 一张瓦片都没解出来:成果会是全 nodata 的空高程图,后续等高线/地形切片
    # 也全是平地。这种情况必须报错而不是产出一份"看起来正常"的空文件。
    if decoded == 0:
        out_path.unlink(missing_ok=True)
        raise RuntimeError(
            f"第 {tr.z} 级共 {tr.count} 张瓦片全部无高程数据,无法拼接。"
            "该数据源在此范围的最高可用级别低于所选级别,请改用更低的级别。")

    return out_path


def hillshade_from_dem(
    dem_path: Path,
    out_path: Path,
    azimuth: float = 315.0,
    altitude: float = 45.0,
    z_factor: float = 1.0,
    build_overviews: bool = True,
) -> Path:
    """从高程 GeoTIFF 本地生成灰度晕渲图(hillshade)GeoTIFF。

    用标准 Horn 3x3 邻域算法计算坡度/坡向,再按光照方向着色(0-255 灰度)。
    不依赖 GDAL 命令行,纯 numpy 实现,输出与源同坐标系/同网格。

    azimuth: 光源方位角(度,默认 315=西北);altitude: 光源高度角(度,默认 45)。
    z_factor: 垂直夸张系数(高程与平面单位不同或需强化地形时调大)。
    """
    with rasterio.open(dem_path) as src:
        elev = src.read(1).astype(np.float64)
        transform = src.transform
        profile = src.profile.copy()
        nodata = src.nodata
        crs = src.crs

    # 像元地面尺寸(米):EPSG:3857 下 transform 的像元宽高即为米
    xres = abs(transform.a)
    yres = abs(transform.e)

    # nodata 先填为邻域可计算的值(用有效区均值),最后再遮罩回去
    mask = None
    if nodata is not None:
        mask = elev == nodata
        if mask.all():
            fill = 0.0
        else:
            fill = float(elev[~mask].mean())
        elev = np.where(mask, fill, elev)

    # Horn 算法:3x3 窗口的东西/南北梯度
    z = elev * z_factor
    # np.gradient 返回 (d/drow, d/dcol);行向南为正,列向东为正
    dzdy, dzdx = np.gradient(z, yres, xres)

    slope = np.arctan(np.hypot(dzdx, dzdy))
    aspect = np.arctan2(dzdy, -dzdx)

    az = np.radians(360.0 - azimuth + 90.0)
    alt = np.radians(altitude)

    shaded = (
        np.sin(alt) * np.cos(slope)
        + np.cos(alt) * np.sin(slope) * np.cos(az - aspect)
    )
    shaded = np.clip(shaded, 0.0, 1.0)
    hs = (shaded * 255.0).astype(np.uint8)

    if mask is not None:
        hs[mask] = 0

    profile.update({
        "count": 1,
        "dtype": "uint8",
        "nodata": 0,
        "compress": "LZW",
        "crs": crs,
        # 大范围晕渲图仍可能超 4GB 普通 TIFF 上限,用 BIGTIFF 突破
        "BIGTIFF": "YES",
    })

    out_path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(out_path, "w", **profile) as dst:
        dst.write(hs, 1)
        if build_overviews:
            dst.build_overviews([2, 4, 8, 16], Resampling.average)
            dst.update_tags(ns="rio_overview", resampling="average")

    return out_path
