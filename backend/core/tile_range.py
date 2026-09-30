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


def supports_global_basemap(provider: str) -> bool:
    """该数据源是否支持全球底图(切片包低层级用真实全球瓦片)。

    **DEM 与本地/建筑源不支持**:它们的 TMS/OSM 导出走另一条链路
    (`_tms_from_dem` / `_tms_from_source_file`),全球段根本进不去 ——
    不拦的话下载阶段会白下 geodetic z1-5 的 682 张,预估也白算进总数,
    而导出时一张都不出现(静默浪费,用户看不出原因)。
    """
    if provider in ("local_dem", "local_image"):
        return False
    from ..providers.buildings import is_building_provider
    if is_building_provider(provider):
        return False
    # 按 DataKind 判定而非 provider 名单:DEM 的名单(providers/terrain.py 的
    # DEM_PROVIDERS)只有当前在用的那个,而 formats 的登记表才是全量
    # (aws_terrain 这类停用但仍在库里的也在其中)。
    from .formats import DataKind, kind_of
    return kind_of(provider) != DataKind.RASTER_DEM