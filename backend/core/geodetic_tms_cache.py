"""mercator 源(Google/Esri)的 geodetic TMS 全球底图 —— 重投影一次,缓存复用。

全球低层级是**固定内容**(与任务范围无关),所以转成 geodetic 后放
{cache_dir}/{provider_key}_geodetic_tms/ 长期复用,后续任务零成本。
转法:把 mercator 低层级瓦片拼成一张 3857 源图,再走 tms.export_tms_from_source
重投影(它输出 TMS 格式,级号 z−1 —— 与本目录命名一致,直接可被 _stage_tms 合并)。
"""
from __future__ import annotations

from pathlib import Path

from . import gdal_env  # noqa: F401
from .mercator_tiling import LAT_LIMIT, mercator_range_for_bbox
from .mosaic import mosaic_to_geotiff
from .tms import export_tms_from_source


def ensure_geodetic_tms_cache(
    cache_dir: Path, provider_key: str, global_max_level: int,
    bbox_global, tile_path_fn, provider, levels_for_src,
) -> Path:
    """确保低层级 geodetic TMS 缓存存在并返回其目录;缺失时生成一次。

    levels_for_src:参与拼源的 mercator 层级(通常 = [global_max_level],
      用最低一层作源,再由 export_tms_from_source 降采样补出更低级)。
    """
    target = cache_dir / f"{provider_key}_geodetic_tms"
    marker = target / "_ready.txt"
    if marker.exists():
        return target

    target.mkdir(parents=True, exist_ok=True)
    src_z = max(levels_for_src)
    tr = mercator_range_for_bbox(*bbox_global, src_z)
    tmp_src = target / f"_src_z{src_z}.tif"
    mosaic_to_geotiff(provider, cache_dir, tr, tmp_src, tile_path_fn,
                      crs="EPSG:3857")
    export_tms_from_source(
        tmp_src, bbox_global,
        list(range(1, global_max_level + 1)), target,
        fill_to_tms_zero=True, on_progress=lambda *_: None)
    tmp_src.unlink(missing_ok=True)
    marker.write_text("ok", encoding="utf-8")
    return target