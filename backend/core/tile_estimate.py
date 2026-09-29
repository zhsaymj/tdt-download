"""该任务要下载多少张原始瓦片 —— **唯一判定处**。

建任务时(预估、落库 `total`)与运行期(下载阶段进度的分母)必须用同一个口径。
此前两处各算各的,于是出现需求39 那个现象:

    进度显示 100% 却还在下载,而且**下载数大于总数**

成因是分母在**建任务时**算好落库,而运行期实际要下的量更大 —— 天地图
tms+osm 同选时会下两套原生瓦片(见 `core.formats.download_grids_of`),
补下另一套时 `total` 没跟着变。

口径 = 按**实际要下载的网格**各算一遍再相加;开了注记时,对 ≤ANNOTATION_MAX_Z
的级别翻倍 —— 与下载阶段的实现一致(注记复用底图算出的同一个 `TileRange`,
两者行列号同源,故张数相同)。
"""
from __future__ import annotations

from .annotate import ANNOTATION_MAX_Z
from .formats import GEO_MERCATOR, download_grids_of
from .mercator_tiling import mercator_range_for_bbox
from .tiling import range_for_bbox


def grids_of(provider: str, formats) -> list[str]:
    """该任务实际要下载的网格(接受逗号分隔字符串或格式列表)。"""
    if isinstance(formats, str) or formats is None:
        from ..models import parse_export
        formats = parse_export(formats or "")
    return download_grids_of(provider, formats or [])


def _level_count(bbox, z: int, grid: str) -> int:
    """单个网格、单个级别的瓦片数。"""
    if grid == GEO_MERCATOR:
        return mercator_range_for_bbox(*bbox, z).count
    return range_for_bbox(*bbox, z).count


def tile_total(provider: str, formats, bbox, levels,
               annotate: bool = False) -> int:
    """该任务要下载的原始瓦片总数(含注记)。

    annotate 为真时,≤ `ANNOTATION_MAX_Z` 的级别按"每张底图瓦片配一张注记瓦片"
    翻倍。调用方自己保证 DEM/本地文件源传 False(它们没有注记)。
    """
    total = 0
    for grid in grids_of(provider, formats):
        for z in (levels or []):
            n = _level_count(bbox, z, grid)
            if annotate and z <= ANNOTATION_MAX_Z:
                n *= 2
            total += n
    return total
