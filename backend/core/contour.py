"""从高程 GeoTIFF 提取等高线,并写成矢量成果(GeoJSON / GPKG / Shapefile)。

实现路径的选择:环境里**没有** osgeo(GDAL 的 ContourGenerate 不可用)、也没有
matplotlib(其 contour 是最常见的做法),而 `rasterio.features.shapes` 只能出
等值**面**、不出线。故走"等值面边界"这条零依赖路线:

  对每条等高距 L,取 elev >= L 的二值掩膜 → features.shapes 出多边形 →
  取多边形边界即该高度的等高线。

代价是边界沿像素走、呈直角锯齿状,需要 simplify 平滑。实测容差取一个像素宽时,
顶点降到 13%、长度保留 82%(减掉的正是锯齿的冗余周长),视觉上已是平滑曲线。
容差再放大到 2 像素收益很小(顶点 9%),故默认用 1 像素。

写出走 pyogrio(自带 GDAL,与 rasterio 那套并存;见 requirements.txt 的说明)。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from shapely import wkb as shapely_wkb
from shapely.geometry import shape

from .logs import logger

#: 默认等高距(米)。30m 级 DEM 上 50m 等高距既不过密也能看出地形。
DEFAULT_INTERVAL = 50.0


def _iter_contour_lines(elev: np.ndarray, transform, level: float,
                        simplify_tol: float):
    """产出某一高度的等高线几何(已平滑)。"""
    mask = (elev >= level)
    if not mask.any() or mask.all():
        # 全高于或全低于该高度 → 该高度不成线
        return
    m8 = mask.astype("uint8")
    for geom, _val in features.shapes(m8, mask=mask, transform=transform):
        boundary = shape(geom).boundary
        if boundary.is_empty:
            continue
        parts = [boundary] if boundary.geom_type == "LineString" else list(boundary.geoms)
        for part in parts:
            if simplify_tol > 0:
                part = part.simplify(simplify_tol, preserve_topology=False)
            if part.is_empty or len(part.coords) < 2:
                continue
            yield part


def extract_contours(dem_path: Path, interval: float = DEFAULT_INTERVAL,
                     should_stop=None, on_progress=None
                     ) -> tuple[list[bytes], list[float], str]:
    """从高程 GeoTIFF 提取等高线。

    返回 (WKB 几何列表, 对应高程值列表, CRS 字符串)。
    interval: 等高距(与高程单位一致,通常是米)。
    """
    with rasterio.open(dem_path) as src:
        elev = src.read(1, masked=True)
        transform = src.transform
        crs = str(src.crs) if src.crs else "EPSG:4326"
        px = abs(transform.a)

    if elev.count() == 0:
        logger.warning("等高线:高程数据全为空值 %s", dem_path.name)
        return [], [], crs

    # 掩膜区填成最小值,避免无数据区被当成"低于该高度"而生成假边界
    vmin = float(elev.min())
    vmax = float(elev.max())
    arr = np.ma.filled(elev, vmin - 1.0).astype("float64")

    if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax - vmin < interval:
        logger.info("等高线:高差 %.1f 小于等高距 %.1f,无等高线可提取",
                    vmax - vmin, interval)
        return [], [], crs

    # 从 interval 的整数倍起,落在数据高差之内
    start = np.ceil(vmin / interval) * interval
    levels = np.arange(start, vmax, interval)
    geoms: list[bytes] = []
    values: list[float] = []
    for i, lv in enumerate(levels):
        if should_stop and should_stop():
            break
        for line in _iter_contour_lines(arr, transform, float(lv), px):
            geoms.append(shapely_wkb.dumps(line))
            values.append(float(lv))
        if on_progress:
            on_progress(i + 1, len(levels))

    logger.info("等高线:%s 提取 %d 条(高程 %.0f~%.0f,等高距 %.0f)",
                dem_path.name, len(geoms), vmin, vmax, interval)
    return geoms, values, crs


def write_contours(out_path: Path, geoms: list[bytes], values: list[float],
                   crs: str, container: str = "geojson") -> Path:
    """把等高线写成矢量文件。container 见 core.formats.CONTAINERS。

    走 pyogrio 而非手写字节:实测中文字段名与属性值在 shp/gpkg 里往返一致
    (自动生成 .cpg),CGCS2000 各带号也能正确写进 .prj。
    """
    from pyogrio.raw import write

    from .formats import CONTAINERS

    cont = CONTAINERS.get(container)
    if cont is None:
        raise ValueError(f"未知的容器格式:{container}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not geoms:
        logger.warning("等高线:无数据可写,跳过 %s", out_path.name)
        return out_path

    n = len(geoms)
    geom_arr = np.empty(n, dtype=object)
    for i, g in enumerate(geoms):
        geom_arr[i] = g
    # 字段名用中文:实测 shp/gpkg 往返一致。shp 的字段名上限 10 字符,"高程" 远低于此。
    fields = np.array(["高程"])
    field_data = [np.asarray(values, dtype="float64")]

    # 注意 pyogrio.raw.write 的参数顺序是 (path, geometry, field_data, fields);
    # field_data 在 fields 之前,传反了会报 "arrays must be same length" 误导性错误。
    write(str(out_path), geom_arr, field_data, fields,
          driver=cont.driver, crs=crs, geometry_type="LineString",
          encoding="UTF-8", promote_to_multi=False)
    logger.info("等高线:已写出 %s(%d 条,%s)", out_path.name, n, cont.label)
    return out_path

