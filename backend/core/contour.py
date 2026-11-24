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

import math
from pathlib import Path

import numpy as np
import rasterio
from rasterio import features
from rasterio.transform import Affine
from rasterio.windows import Window, bounds
from shapely import wkb as shapely_wkb
from shapely.geometry import box, shape

from .logs import logger

#: 默认等高距(米)。30m 级 DEM 上 50m 等高距既不过密也能看出地形。
DEFAULT_INTERVAL = 50.0

#: 分带提取的行高与上下重叠行数。
#: 为什么必须重叠:features.shapes 出的是等值**区**的多边形,按带切开时区域会在
#: 带边界被截断、沿边界闭合成一条假边界线。带上下各多读 overlap 行后,假边界
#: 落在带边缘而不是核心区内,裁剪到核心区时自然被丢掉;相邻核心区首尾相接,
#: 既不重不漏。
_BAND_ROWS = 1024
_BAND_OVERLAP = 64


def _band_spans(height: int):
    """产出 (核心区起始行, 核心区行数, 读取起始行, 读取结束行)。"""
    for y0 in range(0, height, _BAND_ROWS):
        core_h = min(_BAND_ROWS, height - y0)
        top = max(0, y0 - _BAND_OVERLAP)
        bottom = min(height, y0 + core_h + _BAND_OVERLAP)
        yield y0, core_h, top, bottom


def _iter_band_contour_lines(band: np.ndarray, transform, level: float,
                             simplify_tol: float, valid=None, clip=None):
    """产出一"带"内某一高度的等高线几何(已裁剪到核心区、已平滑)。"""
    mask = (band >= level)
    if valid is not None:
        mask &= valid
    if not mask.any() or mask.all():
        # 全高于或全低于该高度 → 该高度不成线
        return
    # bool 与 uint8 同为 1 字节,view 零拷贝 —— 全国级栅格每多一次 astype
    # 就是上 GB 的瞬时分配。
    m8 = mask.view(np.uint8)
    for geom, _val in features.shapes(m8, mask=mask, transform=transform):
        boundary = shape(geom).boundary
        if clip is not None:
            boundary = boundary.intersection(clip)
        if boundary.is_empty:
            continue
        if boundary.geom_type == "LineString":
            parts = [boundary]
        elif hasattr(boundary, "geoms"):
            parts = [g for g in boundary.geoms if g.geom_type == "LineString"]
        else:
            continue
        for part in parts:
            if simplify_tol > 0:
                part = part.simplify(simplify_tol, preserve_topology=False)
            if part.is_empty or len(part.coords) < 2:
                continue
            yield part


def extract_contours(dem_path: Path, interval: float = DEFAULT_INTERVAL,
                     should_stop=None, on_progress=None, on_chunk=None
                     ) -> tuple[list[bytes], list[float], str]:
    """从高程 GeoTIFF 提取等高线。

    返回 (WKB 几何列表, 对应高程值列表, CRS 字符串)。
    interval: 等高距(与高程单位一致,通常是米)。
    on_progress(done_bands, total_bands): 按"带"上报 —— 分带提取的自然刻度。
    on_chunk(geoms, values, crs): 传入则**流式**交付每一带的结果,不再累积到
    返回值(返回的列表为空)。等高线每条 WKB 约 1.5 KB,全国级可达数十万条,
    全量累积是 MB~GB 级;流式交付让峰值与总条数无关。

    **必须分带**:早期实现是 src.read(1, masked=True) 后
    np.ma.filled(...).astype("float64")。全国 z13 高程图 46336x29440 即
    1.36G 像素,float32 已占 5.08GB,再转 float64 又要 10.9GB,叠加后直接撞穿
    内存上限(与 reproject_geotiff 的 OOM 同一个来源)。现在每次只读一条
    _BAND_ROWS 高的带,峰值与整幅尺寸无关。
    """
    with rasterio.open(dem_path) as src:
        transform = src.transform
        crs = str(src.crs) if src.crs else "EPSG:4326"
        px = abs(transform.a)
        width, height = src.width, src.height
        nodata = src.nodata
        spans = list(_band_spans(height))

        # 第一遍:分带求极值。不能整幅 read(1) —— 那正是 OOM 的源头。
        vmin, vmax = math.inf, -math.inf
        found = False
        for _y0, _core_h, top, bottom in spans:
            band = src.read(1, window=Window(0, top, width, bottom - top))
            if nodata is not None:
                band = band[band != nodata]
            if band.size:
                found = True
                vmin = min(vmin, float(band.min()))
                vmax = max(vmax, float(band.max()))

        if not found:
            logger.warning("等高线:高程数据全为空值 %s", dem_path.name)
            return [], [], crs

        if not np.isfinite(vmin) or not np.isfinite(vmax) or vmax - vmin < interval:
            logger.info("等高线:高差 %.1f 小于等高距 %.1f,无等高线可提取",
                        vmax - vmin, interval)
            return [], [], crs

        # 从 interval 的整数倍起,落在数据高差之内
        start = np.ceil(vmin / interval) * interval
        levels = np.arange(start, vmax, interval)

        # 第二遍:逐带读入并对所有等高距一次算完(只扫一遍 IO)。
        # 无数据区用 valid 掩膜排除,等价于早期"填成 vmin-1"的做法 ——
        # 填值会多分配一份整幅数组,内存翻倍。
        geoms: list[bytes] = []
        values: list[float] = []
        emitted = 0
        for bi, (y0, core_h, top, bottom) in enumerate(spans, start=1):
            if should_stop and should_stop():
                break
            band = src.read(1, window=Window(0, top, width, bottom - top))
            valid = (band != nodata) if nodata is not None else None
            # 把原点下移 top 行。e(像素高)为负,故平移量取 +top:
            # f' = f + e*top 正是"第 top 行的左上角纬度"。
            band_tf = transform * Affine.translation(0, top)
            left, bottom_g, right, top_g = bounds(
                Window(0, y0 - top, width, core_h), band_tf)
            clip = box(left, bottom_g, right, top_g)
            chunk_geoms: list[bytes] = []
            chunk_values: list[float] = []
            for lv in levels:
                for line in _iter_band_contour_lines(
                        band, band_tf, float(lv), px, valid=valid, clip=clip):
                    chunk_geoms.append(shapely_wkb.dumps(line))
                    chunk_values.append(float(lv))
            emitted += len(chunk_geoms)
            if on_chunk is not None:
                # 本带结果立刻交付写出,不再进总列表 —— 峰值只与单带条数有关
                if chunk_geoms:
                    on_chunk(chunk_geoms, chunk_values, crs)
            else:
                geoms.extend(chunk_geoms)
                values.extend(chunk_values)
            if on_progress:
                on_progress(bi, len(spans))

        logger.info("等高线:%s 提取 %d 条(高程 %.0f~%.0f,等高距 %.0f)",
                    dem_path.name, emitted, vmin, vmax, interval)
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


class ContourWriter:
    """等高线增量写盘:攒够一批就 append 到目标容器,内存只留一个批次。

    pyogrio 的 write 是整份写出,故这里按 _BATCH 条分批落盘:首批覆盖写,
    其后 append=True(实测 GeoJSON / GPKG / Shapefile 三种容器均支持追加)。
    配合 extract_contours(on_chunk=...) 使用,峰值与等高线总条数无关。
    """

    _BATCH = 50_000

    def __init__(self, out_path: Path, container: str = "geojson"):
        from .formats import CONTAINERS

        cont = CONTAINERS.get(container)
        if cont is None:
            raise ValueError(f"未知的容器格式:{container}")
        self.out_path = out_path
        self.cont = cont
        self._geoms: list[bytes] = []
        self._values: list[float] = []
        self._crs: str | None = None
        self._wrote = 0
        self.out_path.parent.mkdir(parents=True, exist_ok=True)

    def add(self, geoms, values, crs: str | None = None) -> None:
        """接收一批 (WKB, 高程值);crs 取首次传入的值。"""
        if crs and not self._crs:
            self._crs = crs
        self._geoms.extend(geoms)
        self._values.extend(values)
        if len(self._geoms) >= self._BATCH:
            self._flush()

    def _flush(self) -> None:
        if not self._geoms:
            return
        from pyogrio.raw import write

        n = len(self._geoms)
        geom_arr = np.empty(n, dtype=object)
        for i, g in enumerate(self._geoms):
            geom_arr[i] = g
        # 参数顺序 (path, geometry, field_data, fields),见 write_contours 的注释
        write(str(self.out_path), geom_arr,
              [np.asarray(self._values, dtype="float64")],
              np.array(["高程"]),
              driver=self.cont.driver, crs=self._crs or "EPSG:4326",
              geometry_type="LineString", encoding="UTF-8",
              promote_to_multi=False, append=self._wrote > 0)
        self._wrote += n
        self._geoms.clear()
        self._values.clear()

    @property
    def written(self) -> int:
        """已落盘 + 尚在缓冲的条目总数。"""
        return self._wrote + len(self._geoms)

    def close(self) -> Path:
        self._flush()
        if not self._wrote:
            logger.warning("等高线:无数据可写,跳过 %s", self.out_path.name)
        else:
            logger.info("等高线:已写出 %s(%d 条,%s)", self.out_path.name,
                        self._wrote, self.cont.label)
        return self.out_path

