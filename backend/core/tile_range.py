"""「第 z 层实际下载/导出的瓦片区间」—— 唯一判定处。

下载、预估、进度、导出都从这里取值。全球底图(global_max_level)与边缘缓冲
(buffer_rings)收敛在此,下游不得各自按 bbox 另算区间 —— 分家必然漂移
(需求39:进度 100% 还在下载,根因就是两处各算各的)。
"""
from __future__ import annotations

from .formats import GEO_MERCATOR
from .mercator_tiling import mercator_range_for_bbox
from .tiling import TileRange, matrix_size, range_for_bbox


def _range_fn_for(grid: str):
    """geodetic/mercator 各自的区间函数;行号语义不同,混用即整体错位。"""
    return mercator_range_for_bbox if grid == GEO_MERCATOR else range_for_bbox


def _matrix_limits(z: int, grid: str) -> tuple[int, int]:
    """第 z 层的 (列数, 行数)。geodetic 行数 2^(z-1),mercator 行数 2^z。"""
    if grid == GEO_MERCATOR:
        return 2 ** z, 2 ** z
    return matrix_size(z)


def level_range(z, grid, bbox, *, global_max_level=0, buffer_rings=0) -> TileRange:
    """第 z 层实际要下载/导出的瓦片区间。

    z ≤ global_max_level → 该层整层(全球底图)
    否则                  → 范围相交区间外扩 buffer_rings 圈,clamp 到层边界
    """
    if global_max_level > 0 and z <= global_max_level:
        ncols, nrows = _matrix_limits(z, grid)
        return TileRange(z, 0, ncols - 1, 0, nrows - 1)
    base = _range_fn_for(grid)(*bbox, z)
    if buffer_rings <= 0:
        return base
    ncols, nrows = _matrix_limits(z, grid)
    return TileRange(
        z,
        max(0, base.col_min - buffer_rings), min(ncols - 1, base.col_max + buffer_rings),
        max(0, base.row_min - buffer_rings), min(nrows - 1, base.row_max + buffer_rings),
    )


def download_levels(levels, global_max_level=0) -> list[int]:
    """实际要下载的层级集合 = 用户勾选 ∪ {1..global_max_level},升序无重复。"""
    if global_max_level <= 0:
        return sorted(levels)
    return sorted(set(levels) | set(range(1, global_max_level + 1)))