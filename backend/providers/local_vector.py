"""本地上传矢量面(房屋轮廓)→ 三维建筑白模数据源。

与 Overpass/Overture 的区别:数据不来自网络,而是用户上传的 shp/geojson/kml
(前端已解析并转成 WGS84 GeoJSON,存于 data/uploads/<id>.geojson)。

两个由用户决定的映射:
  1. **高度**:从哪个属性字段取。支持三种模式
     - meters:字段值即高度(米),可乘换算系数(如数据以厘米/英尺存)
     - floors:字段值是层数,按 层数 × 层高 折算
     - none  :不用字段,全部走上层的兜底/面积估算逻辑
     字段值允许是 "12.5 米" 这类带单位文本(parse_number 会取出数值)。
  2. **保留字段**:哪些属性写进 b3dm 的 Batch Table(供 Cesium 点击拾取查看)。
     放进 BuildingFeature.props,由 core/b3dm.py 动态并入 Batch Table。
"""
from __future__ import annotations

from typing import Iterator

from ..core.logs import logger
from ..core.vector_upload import load_upload, parse_number
from .buildings import Building3DSource, BuildingFeature


class HeightMode:
    METERS = "meters"      # 字段值为高度(米)
    FLOORS = "floors"      # 字段值为层数
    NONE = "none"          # 不取字段,交由上层兜底
    ALL = (METERS, FLOORS, NONE)


class LocalVectorSource(Building3DSource):
    """本地上传的房屋轮廓面。"""

    key = "local_vector"
    label = "本地矢量面(上传)"

    def __init__(
        self,
        upload_id: str,
        *,
        height_field: str = "",
        height_mode: str = HeightMode.NONE,
        height_scale: float = 1.0,
        floor_height: float = 3.0,
        name_field: str = "",
        keep_fields: list[str] | None = None,
    ):
        if height_mode not in HeightMode.ALL:
            raise ValueError(f"未知高度模式:{height_mode}")
        self.upload_id = upload_id
        self.height_field = height_field or ""
        self.height_mode = height_mode
        self.height_scale = float(height_scale or 1.0)
        self.floor_height = float(floor_height or 3.0)
        self.name_field = name_field or ""
        self.keep_fields = list(keep_fields or [])

    # ---------- 内部 ----------

    def _resolve(self, props: dict) -> tuple[float | None, int | None]:
        """按用户映射从属性里取 (height_m, num_floors)。

        返回的 height 交给 core/buildings.prepare_buildings 做区间校验与兜底;
        这里只负责按用户选择取值和换算,不做合理性判断。
        """
        if self.height_mode == HeightMode.NONE or not self.height_field:
            return None, None
        raw = props.get(self.height_field)
        val = parse_number(raw)
        if val is None:
            return None, None
        if self.height_mode == HeightMode.FLOORS:
            floors = int(round(val * self.height_scale))
            if floors <= 0:
                return None, None
            # 同时给出 height 与 floors:上层优先用 height,floors 仅作记录
            return floors * self.floor_height, floors
        return val * self.height_scale, None

    def _kept(self, props: dict) -> dict:
        """挑出用户选择保留的字段(缺失的跳过,不塞空占位)。"""
        if not self.keep_fields:
            return {}
        out = {}
        for k in self.keep_fields:
            if k in props:
                v = props[k]
                if v is None:
                    continue
                # 只保留标量;嵌套结构无法写进 Batch Table
                out[k] = v if isinstance(v, (int, float, str)) else str(v)
        return out

    # ---------- 对外接口 ----------

    def fetch(self, bbox, *, on_progress=None, should_stop=None) -> Iterator[BuildingFeature]:
        """读取上传数据,产出建筑要素。

        bbox 在此仅作**过滤**用(与网络数据源不同,数据是现成的):
        落在范围外的要素跳过,便于用大文件只切其中一片。
        """
        gj = load_upload(self.upload_id)
        west, south, east, north = bbox
        feats = gj.get("features") if str(gj.get("type") or "").lower() == "featurecollection" \
            else [gj]

        emitted = 0
        skipped_outside = 0
        no_height = 0
        for i, feat in enumerate(feats or []):
            if should_stop and should_stop():
                logger.info("本地矢量取数被中断,已产出 %d 栋", emitted)
                return
            if not feat:
                continue
            geom = feat.get("geometry") or {}
            props = feat.get("properties") or {}
            for rings in _iter_rings(geom):
                if not rings or len(rings[0]) < 3:
                    continue
                # bbox 过滤:用外环的包围盒相交判断(比逐点判断快且不漏边界建筑)
                xs = [p[0] for p in rings[0]]
                ys = [p[1] for p in rings[0]]
                if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                    skipped_outside += 1
                    continue

                h, floors = self._resolve(props)
                if h is None:
                    no_height += 1
                name = ""
                if self.name_field:
                    nv = props.get(self.name_field)
                    name = "" if nv is None else str(nv)

                yield BuildingFeature(
                    fid=str(props.get("id") or props.get("ID") or f"local/{i}"),
                    rings=rings,
                    height=h,
                    num_floors=floors,
                    name=name,
                    props=self._kept(props),
                )
                emitted += 1
            if on_progress and emitted and emitted % 500 == 0:
                on_progress(emitted, None)

        logger.info("本地矢量取数完成:产出 %d 栋(范围外跳过 %d,无有效高度 %d)",
                    emitted, skipped_outside, no_height)

    def count(self, bbox) -> int:
        """面要素总数(不做 bbox 过滤,仅供进度分母的粗略值)。"""
        try:
            gj = load_upload(self.upload_id)
        except Exception as e:
            logger.warning("本地矢量计数失败:%s", str(e)[:200])
            return 0
        feats = gj.get("features") if str(gj.get("type") or "").lower() == "featurecollection" \
            else [gj]
        n = 0
        for f in feats or []:
            gt = ((f or {}).get("geometry") or {}).get("type")
            if gt == "Polygon":
                n += 1
            elif gt == "MultiPolygon":
                n += len(((f or {}).get("geometry") or {}).get("coordinates") or [])
        return n


def _iter_rings(geom: dict):
    """从 GeoJSON 几何取出 [外环, 洞...];MultiPolygon 逐个 yield。

    环坐标去掉闭合重复末点(与 BuildingFeature 的约定一致)。
    """
    gtype = geom.get("type")
    if gtype == "Polygon":
        polys = [geom.get("coordinates") or []]
    elif gtype == "MultiPolygon":
        polys = geom.get("coordinates") or []
    else:
        return

    for poly in polys:
        rings = []
        for ring in poly or []:
            coords = [(float(c[0]), float(c[1])) for c in ring or []
                      if c is not None and len(c) >= 2]
            if len(coords) >= 2 and abs(coords[0][0] - coords[-1][0]) < 1e-12 \
                    and abs(coords[0][1] - coords[-1][1]) < 1e-12:
                coords = coords[:-1]
            if len(coords) >= 3:
                rings.append(coords)
        if rings:
            yield rings


def build_local_vector_source(upload_id: str, **kw) -> LocalVectorSource:
    if not upload_id:
        raise ValueError("缺少上传数据 id,请先上传矢量面数据")
    return LocalVectorSource(upload_id, **kw)
