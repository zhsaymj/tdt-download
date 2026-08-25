"""修复旧成果的 nodata=0 白点问题(原地改元数据,不重写像素)。

背景:早期 clip_to_geometry 用 nodata=0 表达"选区外"。但三波段 uint8 影像里
0 是合法像素值——深色植被的红波段经常正好取 0(实测 z13 有 6.5% 的有效像素
至少有一个波段为 0)。QGIS 按波段套用 nodata,选区**内部**这些像素也被判成
无数据、渲成白点,看着像影像丢了像素。

修复办法:清掉 nodata 声明,改用 GDAL 掩膜波段表达有效区。像素值一个都不动,
只加一个 1bit 掩膜(实测 +0.2% 体积、单张 0.1s),故不必重新下载。

有效区判据是"三波段全 0 = 无数据"。这对本工具的成果是安全的:缺失瓦片按整块
256×256 填黑、选区外是连通的整片黑,而真实影像里三波段同时恰好为 0 的孤立像素
几乎不存在(实测 z13 全黑区无一个被有效像素包围的孤立点)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio

from .logs import logger

#: 单次处理的最大像素数。z18 级成果实测 77568×54272 = 42 亿像素,
#: 光是整幅 uint8 掩膜数组就要 4.2GB、三波段全读要 12.6GB,必须分块流式处理。
_CHUNK_PIXELS = 64 * 1024 * 1024


def needs_repair(path: Path) -> bool:
    """判断该栅格是否受 nodata=0 白点问题影响。

    仅针对多波段 uint8(影像/晕渲 RGB)。单波段高程的 nodata 是真实的
    (-32768 之类),不能动。
    """
    try:
        with rasterio.open(path) as ds:
            return (
                ds.count >= 3
                and ds.dtypes[0] == "uint8"
                and ds.nodata is not None
                and float(ds.nodata) == 0.0
            )
    except Exception:
        return False


def repair_raster(path: Path) -> tuple[bool, int]:
    """原地修复一张栅格:清 nodata=0 + 写掩膜波段。

    返回 (是否修复, 恢复的像素数)。不需要修复或失败时返回 (False, 0)。
    """
    if not needs_repair(path):
        return False, 0

    recovered = 0
    try:
        # 流式:逐块读像素 → 算掩膜 → 立即按窗口写回,不留整幅数组。
        # z18 成果 42 亿像素,整幅掩膜就要 4.2GB。
        with rasterio.open(path, "r+") as ds:
            height, width = ds.height, ds.width
            rows_per_chunk = max(1, min(height, _CHUNK_PIXELS // max(1, width)))
            # 对齐到 256 的整数倍:成果按 256×256 分块存储,块内对齐能避免
            # 读写跨块产生额外 IO
            rows_per_chunk = max(256, (rows_per_chunk // 256) * 256)
            # nodata 必须先清:留着它,下面 read() 会走 masked 逻辑,
            # 且写完掩膜后再清会让部分 GDAL 版本把掩膜当成 nodata 的派生物
            ds.nodata = None
            for y0 in range(0, height, rows_per_chunk):
                h = min(rows_per_chunk, height - y0)
                win = rasterio.windows.Window(0, y0, width, h)
                arr = ds.read(window=win)
                nonzero = (arr != 0).any(axis=0)
                # 原先会被误判为白点的:有波段为 0 但不全为 0
                recovered += int(((arr == 0).any(axis=0) & nonzero).sum())
                ds.write_mask(np.where(nonzero, 255, 0).astype(np.uint8),
                              window=win)
    except Exception as e:
        logger.warning("修复 nodata 失败(%s):%s", path.name, e)
        return False, 0

    logger.info("已修复 %s:恢复 %d 个被误判的像素", path.name, recovered)
    return True, recovered


def repair_dir(out_dir: Path) -> tuple[int, int]:
    """修复目录下所有受影响的 GeoTIFF。返回 (修复张数, 恢复像素总数)。"""
    fixed = total = 0
    for p in sorted(out_dir.glob("*.tif")):
        ok, n = repair_raster(p)
        if ok:
            fixed += 1
            total += n
    return fixed, total
