"""瓦片拼接 → 带坐标 GeoTIFF(EPSG:4326)。

把 TileRange 覆盖的所有瓦片拼成一张大图,按瓦片区间的地理四至
计算仿射变换,写出带坐标、LZW 压缩、内置概视图的 GeoTIFF。
缺失的瓦片(下载失败)用黑色填充。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds
from rasterio.windows import Window
from rasterio.enums import ColorInterp, Resampling

from ..providers.base import TileProvider
from .annotate import composite_annotation
from .tiling import TILE_SIZE, TileRange


def _read_tile(path: Path, bands: int) -> np.ndarray | None:
    """读单张瓦片为 (bands, 256, 256) 的 uint8 数组;失败返回 None。

    天地图矢量/地形瓦片是 256 色调色板 PNG(单波段索引+色表),
    直接按灰度复制会丢色变黑白,故检测到调色板时用色表展开为 RGB。
    """
    if not (path.exists() and path.stat().st_size > 0):
        return None
    try:
        with rasterio.open(path) as src:
            arr = src.read(out_shape=(src.count, TILE_SIZE, TILE_SIZE))
            # 调色板瓦片:用色表把索引值展开为 RGB
            is_palette = (
                src.count == 1
                and src.colorinterp[0] == ColorInterp.palette
            )
            colormap = src.colormap(1) if is_palette else None
    except Exception:
        return None

    if colormap is not None:
        idx = arr[0].astype(np.uint8)
        # 构建 256×3 查表(色表项为 (R,G,B,A),取 RGB)
        lut = np.zeros((256, 3), dtype=np.uint8)
        for k, v in colormap.items():
            if 0 <= k < 256:
                lut[k] = v[:3]
        rgb = lut[idx]                      # (256,256,3)
        rgb = np.transpose(rgb, (2, 0, 1))  # -> (3,256,256)
        if bands <= 3:
            return rgb[:bands]
        # 需要更多波段(如带 alpha)则补满
        extra = np.full((bands - 3, TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
        return np.concatenate([rgb, extra], axis=0)

    # 统一到目标波段数
    if arr.shape[0] >= bands:
        return arr[:bands].astype(np.uint8)
    # 波段不足(如真灰度)则复制填充
    reps = np.repeat(arr[:1], bands, axis=0)
    return reps.astype(np.uint8)


def mosaic_to_geotiff(
    provider: TileProvider,
    cache_dir: Path,
    tr: TileRange,
    out_path: Path,
    tile_path_fn,
    anno_tile_path_fn=None,
    on_row=None,
) -> Path:
    """拼接指定级别的瓦片为 GeoTIFF。

    tile_path_fn(col, row, z) -> Path,复用下载器的缓存路径规则。
    anno_tile_path_fn(col, row, z) -> Path:可选,提供时把注记瓦片按 alpha
      合成到对应底图瓦片之上(路网注记烘焙进成果)。
    on_row(done_rows, total_rows): 可选,每写完一"瓦片行"回调一次,用于细粒度进度。
    """
    bands = provider.bands
    width = tr.cols * TILE_SIZE
    height = tr.rows * TILE_SIZE

    west, south, east, north = tr.mosaic_bounds()
    transform = from_bounds(west, south, east, north, width, height)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    profile = {
        "driver": "GTiff",
        "height": height,
        "width": width,
        "count": bands,
        "dtype": "uint8",
        "crs": "EPSG:4326",
        "transform": transform,
        "compress": "LZW",
        "tiled": True,
        "blockxsize": 256,
        "blockysize": 256,
        "photometric": "RGB" if bands >= 3 else "MINISBLACK",
        # 大范围/高级别拼图可能超过 4GB 普通 TIFF 上限(32位偏移会写坏文件),
        # 用 BigTIFF 突破;GDAL 会据实际大小写 64 位偏移。
        "BIGTIFF": "YES",
    }

    # 逐"瓦片行"分块写入,而非一次性分配整幅 canvas——否则大范围高级别
    # (如 z18 数万×数万像素)会占几十 GB 内存直接 MemoryError。
    # 每次仅缓存一行瓦片:bands × TILE_SIZE × width。
    total_rows = tr.rows
    with rasterio.open(out_path, "w", **profile) as dst:
        for ri, row in enumerate(range(tr.row_min, tr.row_max + 1), start=1):
            row_buf = np.zeros((bands, TILE_SIZE, width), dtype=np.uint8)
            any_tile = False
            for col in range(tr.col_min, tr.col_max + 1):
                tile = _read_tile(tile_path_fn(col, row, tr.z), bands)
                if tile is None:
                    continue
                # 叠加注记:仅对 RGB 波段合成(前 3 波段)
                if anno_tile_path_fn is not None and bands >= 3:
                    rgb = composite_annotation(tile[:3], anno_tile_path_fn(col, row, tr.z))
                    tile = tile.copy()
                    tile[:3] = rgb
                x = (col - tr.col_min) * TILE_SIZE
                row_buf[:, :, x:x + TILE_SIZE] = tile
                any_tile = True
            # 整行皆缺失也要写(保持黑色填充,与原全图 canvas 语义一致)
            _ = any_tile
            y = (row - tr.row_min) * TILE_SIZE
            dst.write(row_buf, window=Window(0, y, width, TILE_SIZE))
            if on_row:
                on_row(ri, total_rows)
        # 内置概视图,便于 GIS 快速预览
        factors = [2, 4, 8, 16]
        dst.build_overviews(factors, Resampling.average)
        dst.update_tags(ns="rio_overview", resampling="average")

    return out_path
