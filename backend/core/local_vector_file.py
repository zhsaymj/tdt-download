"""本地矢量文件作处理输入源:识别几何类型、转换容器格式。

与已有的 `core/vector_upload.py` 分工不同,不要混淆:
  - vector_upload:**前端**用 shpjs + proj4 解析成 WGS84 GeoJSON 后上传,
    供三维建筑管线取轮廓用(要探测高度字段)。
  - 本模块:**后端**用 pyogrio 直接读原文件,只为把矢量转成别的容器格式
    (shp ↔ gpkg ↔ geojson),不做建模。

之所以现在能走后端读:pyogrio 已因矢量导出而引入(见 requirements.txt),它自带
GDAL,能直接读 shp/gpkg/kml/geojson 并处理各种坐标系,不必再依赖前端解析。

实测(2026-07-31)几个必须处理的情况:
  - `Polygon Z`:带高程维度的面,geometry_type 字符串带 " Z" 后缀
  - `Unknown`:KML 等格式的 geometry_type 要读到要素才知道
  - `EPSG:4979`:三维地理坐标系,转换目标要用二维的 4326
"""
from __future__ import annotations

from pathlib import Path

from .formats import DataKind
from .logs import logger

#: 支持读取的矢量扩展名(与 file_dialog.VECTOR_PATTERNS 对应)
VECTOR_EXTS = {".shp", ".geojson", ".json", ".gpkg", ".kml", ".fgb", ".gml"}


def _base_geom_type(gt: str) -> str:
    """归一化几何类型:去掉 Z/M 维度后缀与 Multi 前缀。

    `Polygon Z` / `MultiPolygon` / `Polygon ZM` 对"该出什么格式"没有区别,
    都是面。保留原始串供界面展示,判定只用归一化结果。
    """
    s = (gt or "").strip()
    for suffix in (" ZM", " Z", " M", "ZM", "Z25D"):
        if s.upper().endswith(suffix.upper()):
            s = s[: -len(suffix)].strip()
            break
    if s.lower().startswith("multi"):
        s = s[5:]
    return s.lower()


def kind_of_geometry(gt: str) -> str | None:
    """几何类型 → DataKind。无法判定时返回 None(交由抽样读要素再判)。"""
    base = _base_geom_type(gt)
    if base in ("polygon",):
        return DataKind.VECTOR_POLYGON
    if base in ("linestring", "line", "curve"):
        return DataKind.VECTOR_LINE
    return None


def probe_geometry_type(path: Path, max_read: int = 50) -> str:
    """读少量要素判断几何类型(geometry_type 为 Unknown 时用,如 KML)。"""
    from pyogrio.raw import read
    from shapely import wkb as swkb

    try:
        _meta, _idx, geoms, _fields = read(str(path), max_features=max_read)
    except Exception as e:
        logger.warning("抽样读取矢量几何失败 %s:%s", path.name, e)
        return ""
    for g in geoms:
        if g is None:
            continue
        try:
            return swkb.loads(g).geom_type
        except Exception:
            continue
    return ""


def inspect_vector(path: Path) -> dict:
    """检查本地矢量文件:几何类型、要素数、字段、CRS、范围,以及可转的容器格式。"""
    from pyogrio import read_bounds, read_info

    from .formats import CONTAINERS, containers_for

    if path.suffix.lower() not in VECTOR_EXTS:
        raise ValueError(
            f"不支持的矢量格式 {path.suffix};支持 "
            + "、".join(sorted(e.lstrip('.') for e in VECTOR_EXTS)))

    info = read_info(str(path))
    raw_gt = str(info.get("geometry_type") or "")
    kind = kind_of_geometry(raw_gt)
    probed = ""
    if kind is None:
        # geometry_type 为 Unknown(KML 等)时读要素再判
        probed = probe_geometry_type(path)
        kind = kind_of_geometry(probed)
    if kind is None:
        shown = probed or raw_gt or "未知"
        raise ValueError(
            f"暂不支持该几何类型:{shown}。目前只支持面(Polygon)与线(LineString)——"
            "点数据转格式意义有限,如有需要可以再加。")

    crs = str(info.get("crs") or "")
    if not crs:
        raise ValueError(
            "该矢量文件没有坐标系信息(CRS),无法定位。"
            "shp 请确认 .prj 文件在同目录,或用 GIS 软件赋予坐标系后重试。")

    # 范围换算到 WGS84(供界面显示与后续按范围裁切)
    bounds_wgs84 = None
    try:
        from rasterio.warp import transform_bounds
        b = read_bounds(str(path))[1]      # (总范围 xmin,ymin,xmax,ymax)
        xmin, ymin, xmax, ymax = (float(b[0].min()), float(b[1].min()),
                                  float(b[2].max()), float(b[3].max()))
        if crs.upper().replace(" ", "") in ("EPSG:4326", "EPSG:4979", "OGC:CRS84"):
            bounds_wgs84 = [xmin, ymin, xmax, ymax]
        else:
            bounds_wgs84 = list(transform_bounds(crs, "EPSG:4326",
                                                 xmin, ymin, xmax, ymax,
                                                 densify_pts=21))
    except Exception as e:
        logger.warning("矢量范围换算失败 %s:%s", path.name, e)

    # fields 是 numpy 数组:不能用 `or []` 兜底(数组的真值判断会抛
    # "truth value of an array with more than one element is ambiguous")
    raw_fields = info.get("fields")
    field_names = ([] if raw_fields is None
                   else [str(f) for f in list(raw_fields)])

    return {
        "path": str(path),
        "filename": path.name,
        "bytes": path.stat().st_size if path.exists() else 0,
        "kind": kind,
        "geometry_type": probed or raw_gt,
        "features": int(info.get("features") or 0),
        "fields": field_names,
        "crs": crs,
        "bounds_wgs84": bounds_wgs84,
        # 矢量只做容器转换,没有"处理阶段"的概念(见 convert_vector 的说明)
        "containers": [
            {"key": c.key, "label": c.label, "ext": c.ext,
             "sidecars": list(c.sidecars), "note": c.note}
            for c in containers_for(kind)
        ],
    }


def convert_vector(src: Path, dst: Path, container: str,
                   to_wgs84: bool = True) -> Path:
    """把矢量文件转成指定容器格式。

    为什么矢量不走"阶段+容器"那套而是单独一个转换函数:栅格的阶段是**真正的处理**
    (拼接、切片、提取等高线),而矢量在这里只是换一种文件装法,没有中间加工。
    硬套阶段模型会凭空多出一个什么都不做的"阶段"。

    to_wgs84:统一转成 WGS84(与工具其余矢量成果一致,前端可直读)。原坐标系是
    投影坐标(CGCS2000 各带号等)时会重投影;已是地理坐标则只去掉 Z/M 维度。
    """
    import numpy as np
    from pyogrio.raw import read, write

    from .formats import CONTAINERS

    cont = CONTAINERS.get(container)
    if cont is None or cont.writer != "pyogrio":
        raise ValueError(f"不支持的矢量容器格式:{container}")

    meta, _idx, geoms, field_data = read(str(src))
    src_crs = str(meta.get("crs") or "EPSG:4326")
    out_crs = "EPSG:4326" if to_wgs84 else src_crs

    if to_wgs84 and src_crs.upper() not in ("EPSG:4326", "OGC:CRS84"):
        geoms = _reproject_wkb(geoms, src_crs, "EPSG:4326")

    dst.parent.mkdir(parents=True, exist_ok=True)
    fields = np.asarray([str(f) for f in meta["fields"]])
    # promote_to_multi:源里混有 Polygon 与 MultiPolygon 时,shp/gpkg 要求单一
    # 几何类型,统一提升为 Multi 才不会写失败
    write(str(dst), geoms, list(field_data), fields,
          driver=cont.driver, crs=out_crs,
          geometry_type=_write_geom_type(meta.get("geometry_type")),
          encoding="UTF-8", promote_to_multi=True)
    logger.info("矢量已转为 %s:%s(%d 个要素)", cont.label, dst.name, len(geoms))
    return dst


def _write_geom_type(gt) -> str:
    """写出时用的几何类型(统一提升为 Multi,见 convert_vector 的说明)。

    不强制去掉 Z:实测源文件的 Z 值是真实高度(如 15.49 米),丢掉是损失数据。
    GDAL 会按实际几何决定是否写 Z,这里只统一 Multi/单一之分。
    """
    base = _base_geom_type(str(gt or ""))
    return {"polygon": "MultiPolygon", "linestring": "MultiLineString"}.get(
        base, "Unknown")


def _reproject_wkb(geoms, src_crs: str, dst_crs: str):
    """把一批 WKB 几何重投影。

    用 rasterio.warp.transform_geom 而非 pyproj:pyproj 不在依赖里(实测 venv 无),
    而 rasterio 已装、其 transform_geom 走同一套 PROJ,实测 CGCS2000 3 度带
    (EPSG:4547)转 WGS84 结果正确。这样不必为矢量重投影再加一个依赖。
    """
    import numpy as np
    from rasterio.warp import transform_geom
    from shapely import wkb as swkb
    from shapely.geometry import mapping, shape

    out = np.empty(len(geoms), dtype=object)
    failed = 0
    for i, g in enumerate(geoms):
        if g is None:
            out[i] = None
            continue
        try:
            geo = transform_geom(src_crs, dst_crs, mapping(swkb.loads(g)))
            out[i] = swkb.dumps(shape(geo))
        except Exception:
            failed += 1
            out[i] = g          # 转不了就保留原样,不因个别要素丢整份数据
    if failed:
        logger.warning("重投影失败 %d 个要素(已保留原坐标)", failed)
    return out

