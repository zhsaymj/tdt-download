"""导出成果的数据说明文件(metadata.json)。

在成果目录根写一个 JSON,描述本次导出的范围、级别、坐标系、
各级瓦片数、导出格式与输出文件清单,便于后续核对与程序解析。
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from ..providers.base import TileProvider
from .dem_tiling import mercator_range_for_bbox as _mercator_range
from .tiling import range_for_bbox

# provider key -> 中文名(与前端展示一致)
PROVIDER_CN = {
    "tianditu_img": "天地图影像",
    "tianditu_vec": "天地图矢量底图",
    "tianditu_ter": "天地图地形晕渲",
    "esri_terrain": "全国地形 DEM(Esri Terrain3D)",
}


#: 网格 → 元数据里的可读描述
_GRID_LABEL = {
    "geodetic": "c (EPSG:4326)",
    "mercator": "XYZ (EPSG:3857 Web Mercator)",
}


def _tile_grids_of(provider_key: str, export_formats) -> list[str]:
    """该任务**实际下载**了哪些网格(单一判定处,见 core.formats)。"""
    from .formats import download_grids_of
    return download_grids_of(provider_key, export_formats or [])


def _tile_grids_label(provider_key: str, export_formats, dem: bool) -> str:
    """tile_matrix_set 字段的可读值。双网格时两套都写出来。

    DEM 保留原来的固定值:DEM 的下载网格语义与影像不同(本地 DEM 不在
    PROVIDER_GRIDS 里,按源推断会得到 geodetic,与既有元数据不符)。
    """
    if dem:
        return _GRID_LABEL["mercator"]
    grids = _tile_grids_of(provider_key, export_formats)
    return " + ".join(_GRID_LABEL.get(g, g) for g in grids)


def per_level_ranges(provider_key: str, export_formats, levels, bbox,
                     dem: bool = False) -> list[dict]:
    """逐级列出瓦片行列区间与数量,便于核对覆盖范围。

    **按实际下载的网格各出一段**,每段带 `grid` 字段 —— 天地图 tms+osm 同选时是
    两套(行列号语义不同,不能混在一行里描述);只勾 osm 时是 mercator,不能再用
    geodetic 的行列号去描述 mercator 的成果(设计 §4:任一处都不得按网格 A 的
    行列号描述网格 B)。

    DEM 保留原来的固定墨卡托口径(本地 DEM 不在 PROVIDER_GRIDS 里,按源推断会得到
    geodetic,与既有元数据不符)。
    """
    if dem:
        plan = [("mercator", _mercator_range)]
    else:
        plan = [(g, _mercator_range if g == "mercator" else range_for_bbox)
                for g in _tile_grids_of(provider_key, export_formats)]
    west, south, east, north = bbox
    out = []
    for grid, range_fn in plan:
        for z in levels:
            tr = range_fn(west, south, east, north, z)
            out.append({
                "grid": grid,
                "level": z,
                "col_min": tr.col_min, "col_max": tr.col_max,
                "row_min": tr.row_min, "row_max": tr.row_max,
                "tile_count": tr.count,
            })
    return out


def write_metadata(
    out_dir: Path,
    *,
    task_id: str,
    name: str,
    provider: TileProvider,
    provider_key: str,
    bbox: tuple[float, float, float, float],
    levels: list[int],
    crs: str,
    export_formats: list[str],
    downloaded: int,
    failed: int,
    total: int,
    outputs: list[str],
    clip: bool,
    annotate: bool = False,
    dem: bool = False,
    hillshade: dict | None = None,
) -> Path:
    """写 metadata.json,返回文件路径。"""
    west, south, east, north = bbox
    # 各级别瓦片行列区间与数量 —— 按**实际下载的网格**各出一段(见 per_level_ranges)
    per_level = per_level_ranges(provider_key, export_formats, levels, bbox, dem)

    meta = {
        "task_id": task_id,
        "name": name,
        "provider": provider_key,
        "provider_name": PROVIDER_CN.get(provider_key, provider_key),
        "tile_matrix_set": _tile_grids_label(provider_key, export_formats, dem),
        # 实际下载的网格列表(天地图 tms+osm 同选时是两套)
        "tile_grids": _tile_grids_of(provider_key, export_formats),
        "data_type": "elevation_dem" if dem else "image",
        # DEM 成果为 Esri Terrain3D LERC 解码后的真实海拔(米,F32)
        "dem_encoding": "Esri Terrain3D LERC (float32 meters)" if dem else None,
        "hillshade_params": hillshade,
        # [minlon, minlat, maxlon, maxlat]
        "bbox_wgs84": [west, south, east, north],
        "levels": levels,
        "level_range": {"min": min(levels), "max": max(levels)} if levels else None,
        "output_crs": crs or ("EPSG:3857" if dem else "EPSG:4326"),
        "clipped_to_geometry": bool(clip),
        "annotation_overlaid": bool(annotate),
        "export_formats": export_formats,
        "tiles": {"total": total, "downloaded": downloaded, "failed": failed},
        "per_level": per_level,
        "outputs": [Path(p).name for p in outputs],
        "generated_at": datetime.now().isoformat(timespec="seconds"),
    }

    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "metadata.json"
    path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    return path
