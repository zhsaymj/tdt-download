"""本地矢量上传的存储与字段探测。

前端已把 shp/geojson/kml 解析并转成 WGS84 GeoJSON(那边有 shpjs + proj4,
能处理 .prj 与 CGCS2000 各带号,后端不必再引入 fiona/pyshp),这里只负责:
  1. 存盘(data/uploads/<id>.geojson),供任务执行时按 id 读取
  2. 探测属性字段:类型、填充率、可解析为数值的比例、样例值
     —— 用户据此选择哪个字段是高度、保留哪些字段

字段探测的意义:房屋轮廓数据的高度字段命名毫无规律(height/HEIGHT/
建筑高度/CENGSHU/floor…),且常有空值或"12.5米"这类带单位的文本。
把统计摆给用户看,比让人盲猜字段名可靠得多。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ..config import settings
from .logs import logger

#: 上传文件保留天数(超过则在下次上传时清理)
UPLOAD_TTL_DAYS = 7
#: 字段样例值个数
SAMPLE_COUNT = 3
#: 单个上传的要素数上限(超出拒收,避免拖垮内存与切片)
MAX_UPLOAD_FEATURES = 500_000

#: 整值数字(可带常见单位后缀),如 "12.5" "30m" "18 米" "6层" "82'"。
#: 必须**整体**匹配:早先用 search 从任意位置抓数字,导致 "J0"、"栋3" 这类
#: 编号被当成数值,字段探测会把编号列误判为可用的高度列、误导用户选错字段。
_STRICT_NUM_RE = re.compile(
    r"^[+-]?\d+(?:\.\d+)?\s*(?:m|M|米|公尺|meters?|metres?|层|层数|F|f|ft|FT|')?$"
)

#: 常见高度/层数字段名(小写比较),用于给出推荐选项
HEIGHT_NAME_HINTS = ("height", "hgt", "gd", "jzgd", "建筑高度", "高度", "檐高", "楼高", "elev_h")
FLOOR_NAME_HINTS = ("floor", "floors", "levels", "cengshu", "cs", "层数", "楼层", "层")


def uploads_dir() -> Path:
    d = settings.abs_path("./data/uploads")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_of(upload_id: str) -> Path:
    # upload_id 由本模块生成(hex),这里仍做一次校验,避免路径穿越
    if not re.fullmatch(r"[0-9a-f]{8,32}", upload_id or ""):
        raise ValueError("非法的上传 id")
    return uploads_dir() / f"{upload_id}.geojson"


def cleanup_old_uploads() -> int:
    """删除超过 TTL 的上传文件,返回删除个数。"""
    cutoff = time.time() - UPLOAD_TTL_DAYS * 86400
    removed = 0
    for p in uploads_dir().glob("*.geojson"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info("清理过期矢量上传 %d 个", removed)
    return removed


def parse_number(value) -> float | None:
    """把字段值解析为数值。支持 "12.5 米" / "30m" 这类带单位文本。

    要求整值即为数字(可带单位后缀):"J0"、"栋3-1" 这类编号返回 None,
    避免把编号列当高度用。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if not s or not _STRICT_NUM_RE.match(s):
        return None
    # 取出数字部分(去掉单位后缀)
    m = re.match(r"^[+-]?\d+(?:\.\d+)?", s)
    if not m:
        return None
    try:
        val = float(m.group(0))
    except ValueError:
        return None
    # 英尺换算为米(仅 ' 与 ft 后缀)
    if s.rstrip().endswith("'") or s.lower().rstrip().endswith("ft"):
        val *= 0.3048
    return val


def _iter_features(geojson: dict):
    t = str(geojson.get("type") or "").lower()
    if t == "featurecollection":
        for f in geojson.get("features") or []:
            if f:
                yield f
    elif t == "feature":
        yield geojson


_POLY_TYPES = ("Polygon", "MultiPolygon")


def analyze(geojson: dict) -> dict:
    """统计要素数、面要素数、bbox 与各属性字段的可用性。

    返回 {feature_count, polygon_count, bbox, fields:[...]}
    fields 每项:{name, filled, fill_rate, numeric, numeric_rate, samples,
                 suggest_height, suggest_floors}
    """
    total = 0
    poly = 0
    minx = miny = 1e18
    maxx = maxy = -1e18
    # 字段名 -> 统计
    stats: dict[str, dict] = {}
    order: list[str] = []

    def scan_coords(c):
        nonlocal minx, miny, maxx, maxy
        if isinstance(c, (int, float)):
            return
        if c and isinstance(c[0], (int, float)):
            x, y = float(c[0]), float(c[1])
            minx = min(minx, x); maxx = max(maxx, x)
            miny = min(miny, y); maxy = max(maxy, y)
        else:
            for sub in c or []:
                scan_coords(sub)

    for feat in _iter_features(geojson):
        total += 1
        if total > MAX_UPLOAD_FEATURES:
            raise ValueError(
                f"要素数超过上限 {MAX_UPLOAD_FEATURES},请先按范围裁分后再上传")
        geom = feat.get("geometry") or {}
        gtype = geom.get("type")
        if gtype in _POLY_TYPES:
            poly += 1
            scan_coords(geom.get("coordinates"))
        props = feat.get("properties") or {}
        for k, v in props.items():
            st = stats.get(k)
            if st is None:
                st = stats[k] = {"filled": 0, "numeric": 0, "samples": []}
                order.append(k)
            if v is None or (isinstance(v, str) and not v.strip()):
                continue
            st["filled"] += 1
            if parse_number(v) is not None:
                st["numeric"] += 1
            if len(st["samples"]) < SAMPLE_COUNT:
                s = v if isinstance(v, (int, float)) else str(v)
                if s not in st["samples"]:
                    st["samples"].append(s)

    fields = []
    for k in order:
        st = stats[k]
        lower = str(k).lower()
        # 数值可解析率高的字段才可能是高度/层数;再结合名称给推荐标记
        num_rate = (st["numeric"] / st["filled"]) if st["filled"] else 0.0
        mostly_numeric = num_rate >= 0.8 and st["filled"] > 0
        fields.append({
            "name": k,
            "filled": st["filled"],
            "fill_rate": round(st["filled"] / total, 4) if total else 0.0,
            "numeric": mostly_numeric,
            "numeric_rate": round(num_rate, 4),
            "samples": st["samples"],
            "suggest_height": bool(
                mostly_numeric and any(h in lower for h in HEIGHT_NAME_HINTS)),
            "suggest_floors": bool(
                mostly_numeric and any(h in lower for h in FLOOR_NAME_HINTS)),
        })

    bbox = None
    if poly and minx < 1e17:
        bbox = [minx, miny, maxx, maxy]

    return {
        "feature_count": total,
        "polygon_count": poly,
        "bbox": bbox,
        "fields": fields,
    }


def save_upload(geojson: dict) -> dict:
    """存盘 + 分析,返回 {upload_id, ...analyze 结果}。"""
    info = analyze(geojson)
    if not info["polygon_count"]:
        raise ValueError("数据中没有面要素(房屋轮廓需为 Polygon/MultiPolygon)")

    cleanup_old_uploads()
    upload_id = uuid.uuid4().hex[:12]
    path = _path_of(upload_id)
    path.write_text(json.dumps(geojson, ensure_ascii=False), encoding="utf-8")
    logger.info("矢量上传已保存:%s(%d 个要素,其中面 %d,字段 %d 个)",
                path.name, info["feature_count"], info["polygon_count"],
                len(info["fields"]))
    return {"upload_id": upload_id, **info}


def load_upload(upload_id: str) -> dict:
    """读回上传的 geojson。"""
    path = _path_of(upload_id)
    if not path.exists():
        raise FileNotFoundError(
            f"上传的矢量数据已不存在(id={upload_id})。"
            f"上传文件保留 {UPLOAD_TTL_DAYS} 天,请重新上传后再建任务。")
    return json.loads(path.read_text(encoding="utf-8"))


def upload_exists(upload_id: str) -> bool:
    try:
        return _path_of(upload_id).exists()
    except ValueError:
        return False
