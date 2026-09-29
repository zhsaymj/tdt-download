"""DEM 专属的级别建议逻辑。

3857 网格数学已抽到 core/mercator_tiling.py(Google/Esri 影像也用它);
本模块只保留 DEM 特有的 suggest_dem_levels,其余名字从那里 re-export
以免破坏现有调用(api/tasks.py、core/runner.py、providers/terrain.py)。

天地图用 EPSG:4326(core/tiling.py),Esri Terrain3D DEM 用 Web 墨卡托 XYZ
(与 core/osm.py 的导出网格同构)。
"""
from __future__ import annotations

from .mercator_tiling import (  # noqa: F401  (re-export,保持旧导入路径可用)
    LAT_LIMIT, MERC_MAX, TILE_SIZE, lonlat_to_xyz, mercator_range_for_bbox,
    mosaic_bounds_3857, tile_bounds_3857,
)
from .mercator_tiling import estimate_mercator_tiles

#: 旧名字:api/tasks.py 正在用。保留别名而非改调用点,减少本次改动面。
estimate_dem_tiles = estimate_mercator_tiles


def suggest_dem_levels(
    bbox: tuple[float, float, float, float],
    z_max_cap: int = 16,
    min_useful_ratio: float = 0.25,
    tile_budget: int = 2000,
    depth: int = 3,
) -> dict:
    """DEM 版的建议级别(判据同 tiling.suggest_levels,换成墨卡托网格算面积)。

    与影像的差别:
      - 网格是 EPSG:3857 墨卡托,列行数与 4326 不同,故面积在 3857 下算
      - 预算更小(2000 张):LERC 瓦片解码 + 拼接比影像慢,且高程成果通常不需要
        叠很多级——一张够精度的高程图比一套金字塔更常用
      - 级别从 0 起(Esri Terrain3D 允许 0 级)

    判据内核已抽到 mercator_tiling.suggest_levels_mercator(影像版共用),
    本函数只负责传 DEM 的参数。行为与抽取前逐字段一致
    (回归护栏见 tests/test_mercator_suggest.py)。
    """
    from .mercator_tiling import suggest_levels_mercator
    return suggest_levels_mercator(bbox, z_max_cap, 0, min_useful_ratio,
                                   tile_budget, depth)
