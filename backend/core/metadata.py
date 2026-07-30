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
    # 各级别瓦片行列区间与数量,便于核对覆盖范围。
    # DEM 用墨卡托 XYZ 网格,其余用天地图 4326 网格。
    range_fn = _mercator_range if dem else range_for_bbox
    per_level = []
    for z in levels:
        tr = range_fn(west, south, east, north, z)
        per_level.append({
            "level": z,
            "col_min": tr.col_min, "col_max": tr.col_max,
            "row_min": tr.row_min, "row_max": tr.row_max,
            "tile_count": tr.count,
        })

    meta = {
        "task_id": task_id,
        "name": name,
        "provider": provider_key,
        "provider_name": PROVIDER_CN.get(provider_key, provider_key),
        "tile_matrix_set": "XYZ (EPSG:3857 Web Mercator)" if dem else "c (EPSG:4326)",
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
