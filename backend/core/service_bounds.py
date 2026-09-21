"""解析本地数据服务的范围，供图层定位。

**项目里有三套互不相同的瓦片网格**，函数签名相似、返回值都是四至元组，
用错了不报错、只静默给出错误范围。改本文件前先读这张表：

| 网格 | 列数 | 行数 | span | 来源模块 |
|---|---|---|---|---|
| 影像 TMS | 2^z | 2^(z-1) | 360/2^z | core/tiling.py |
| Web 墨卡托 | 2^z | 2^z | —— | core/osm.py |
| Cesium 地形 | 2^(L+1) | 2^L | 180/2^L | core/terrain_tiles.py |

地形的 geodetic 与影像的 geodetic **不是同一套网格**，不要复用 tiling.py。
tests/test_service_bounds.py 里有一条回归用例专门锁定这一点。

范围来源按优先级（见 design.md §3.1.1）：

  1. 同级 metadata.json 的 bbox_wgs84        —— 本工具产出都有，最准
  2. tilemapresource.xml 的 BoundingBox      —— 值是瓦片对齐四至，标记为估算
  3. tileset.json 的 root.boundingVolume     —— region（弧度）或 box（ECEF）
  4. layer.json 的 available                 —— **不能用 bounds**，它恒为世界范围
  5. 扫描瓦片索引区间反算                     —— 无元数据的 OSM/XYZ 包的唯一途径
  6. 矢量文件解析几何范围                     —— 服务图层没有任务 bbox 可兜底
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .logs import logger

#: Web 墨卡托世界范围半边长（米）
MERC_MAX = 20037508.342789244
#: Web 墨卡托纬度上限（度）
LAT_LIMIT = 85.05112878

#: 瓦片索引扫描的文件数上限。超限用已扫到的部分并把范围标记为估算值。
MAX_SCAN_FILES = 200_000

#: 瓦片文件名 {col}_{row}.ext（本工具缓存格式）
_COLROW_RE = re.compile(r"^(\d+)_(\d+)$")


def geodetic_bounds(x_min: int, y_min: int, x_max: int, y_max: int,
                    L: int) -> tuple[float, float, float, float]:
    """Cesium 地形网格（列 2^(L+1)、行 2^L、span 180/2^L）的四至。

    y 自南向北（0 在 -90°）。**这是地形专用网格，与影像 TMS 不同。**
    """
    from .terrain_tiles import geodetic_tile_bounds
    w, s, _, _ = geodetic_tile_bounds(x_min, y_min, L)
    _, _, e, n = geodetic_tile_bounds(x_max, y_max, L)
    return (w, s, e, n)


def mercator_bounds(x_min: int, y_min: int, x_max: int, y_max: int,
                    z: int) -> tuple[float, float, float, float]:
    """Web 墨卡托网格的四至，返回值已转成 WGS84 度。

    行列方向：x 自西向东、**y 自北向南**。所以西边取 x_min、东边取 x_max，
    而**北边要取 y_min、南边要取 y_max**——取反了会得到相邻瓦片的共边，
    四至退化成一个零高度的矩形（不报错，只是范围错）。
    """
    from rasterio.warp import transform_bounds

    from .osm import tile_bounds_3857

    minx, _, _, _ = tile_bounds_3857(x_min, y_min, z)          # 西
    _, _, maxx, _ = tile_bounds_3857(x_max, y_min, z)          # 东
    _, _, _, maxy = tile_bounds_3857(x_min, y_min, z)          # 北（y 最小）
    _, miny, _, _ = tile_bounds_3857(x_min, y_max, z)          # 南（y 最大）
    return tuple(transform_bounds(
        "EPSG:3857", "EPSG:4326", minx, miny, maxx, maxy, densify_pts=21))


def detect_grid(tile_dir: Path) -> str:
    """判定瓦片目录的网格：'geodetic'（天地图 TMS）/ 'mercator'（OSM XYZ）。

    判据按可靠性排序：
      1. tilemapresource.xml 的 <TileSets profile="...">——gdal2tiles 明确写了
      2. 第 1 级瓦片的行数：geodetic 只有 1 行、mercator 有 2 行
      3. 都判不出 → mercator（XYZ 是更通用的约定）
    """
    xml = tile_dir / "tilemapresource.xml"
    if xml.is_file():
        try:
            text = xml.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'profile\s*=\s*"([^"]+)"', text)
            if m:
                prof = m.group(1).strip().lower()
                if "geodetic" in prof:
                    return "geodetic"
                if "mercator" in prof:
                    return "mercator"
        except OSError as e:
            logger.debug("读 tilemapresource.xml 失败 %s:%s", xml, e)

    # 第 1 级的行号集合：geodetic 的矩阵是 2×1（行数 2^(z-1)=1，只有 y=0）
    level1 = tile_dir / "1"
    if level1.is_dir():
        row_ids: set[int] = set()
        try:
            for sub in level1.iterdir():
                if sub.is_file():
                    stem = sub.stem
                    m = _COLROW_RE.match(stem)
                    if m:
                        row_ids.add(int(m.group(2)))
                    elif stem.isdigit():
                        row_ids.add(int(stem))
                    continue
                if not sub.is_dir():
                    continue
                for f in sub.iterdir():
                    if not f.is_file():
                        continue
                    m = _COLROW_RE.match(f.stem)
                    if m:
                        row_ids.add(int(m.group(2)))
                    elif f.stem.isdigit():
                        row_ids.add(int(f.stem))
        except OSError:
            pass
        if row_ids:
            return "geodetic" if max(row_ids) == 0 else "mercator"
    return "mercator"


def _scan_level(d: Path, level: int,
                budget: list[int]) -> tuple[int, int, int, int] | None:
    """扫某级别目录，返回 (x_min, y_min, x_max, y_max)；无瓦片返回 None。

    支持两种命名：{x}/{y}.ext（XYZ）与 {col}_{row}.ext（本工具缓存）。
    budget 是可变单元素列表，用作跨级别的文件数配额。
    """
    lv = d / str(level)
    if not lv.is_dir():
        return None
    xs: list[int] = []
    ys: list[int] = []
    try:
        for sub in lv.iterdir():
            if sub.is_file():
                # 扁平命名 {col}_{row}.ext
                m = _COLROW_RE.match(sub.stem)
                if m:
                    xs.append(int(m.group(1)))
                    ys.append(int(m.group(2)))
                    budget[0] -= 1
                    if budget[0] <= 0:
                        break
                continue
            if not sub.is_dir():
                continue
            col = int(sub.name) if sub.name.isdigit() else None
            for f in sub.iterdir():
                if not f.is_file():
                    continue
                m = _COLROW_RE.match(f.stem)
                if m:
                    xs.append(int(m.group(1)))
                    ys.append(int(m.group(2)))
                elif f.stem.isdigit() and col is not None:
                    xs.append(col)
                    ys.append(int(f.stem))
                else:
                    continue
                budget[0] -= 1
                if budget[0] <= 0:
                    break
            if budget[0] <= 0:
                break
    except OSError as e:
        logger.debug("扫瓦片级别失败 %s:%s", lv, e)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def bounds_from_tile_index(tile_dir: Path, grid: str,
                           max_files: int = MAX_SCAN_FILES
                           ) -> tuple[float, float, float, float] | None:
    """扫描瓦片目录反算范围。取**最高级别**的区间——它最贴合实际数据。

    这是没有任何元数据的 OSM/XYZ 瓦片包的唯一定位途径。
    """
    levels = []
    try:
        for p in tile_dir.iterdir():
            if p.is_dir() and p.name.isdigit():
                levels.append(int(p.name))
    except OSError:
        return None
    if not levels:
        return None

    budget = [max_files]
    for z in sorted(levels, reverse=True):
        rng = _scan_level(tile_dir, z, budget)
        if rng is None:
            continue
        x_min, y_min, x_max, y_max = rng
        if grid == "geodetic":
            # 本工具 TMS 缓存目录的行号自南向北，先转成自北向南再做四至
            rows = 2 ** (z - 1) if z >= 1 else 1
            top = rows - 1 - y_max
            bot = rows - 1 - y_min
            return geodetic_bounds(x_min, top, x_max, bot, z)
        return mercator_bounds(x_min, y_min, x_max, y_max, z)
    return None


def bounds_from_metadata(out_dir: Path) -> list[float] | None:
    """同级 metadata.json 的 bbox_wgs84。本工具产出的成果都有，最准。"""
    p = out_dir / "metadata.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("读 metadata.json 失败 %s:%s", p, e)
        return None
    b = d.get("bbox_wgs84")
    if isinstance(b, list) and len(b) == 4 and all(
            isinstance(v, (int, float)) for v in b):
        return [float(v) for v in b]
    return None


def bounds_from_tilemapresource(tile_dir: Path
                                ) -> tuple[list[float], bool] | None:
    """tilemapresource.xml 的 BoundingBox。

    返回 (范围, 是否估算)。属性名是 minx/miny/maxx/maxy；**该值是瓦片对齐
    后的四至（tms.py::write_tilemapresource 用 mosaic_bounds 生成），比真实
    选区每边最多大出一张瓦片**，故恒标记为估算值。
    """
    import xml.etree.ElementTree as ET

    p = tile_dir / "tilemapresource.xml"
    if not p.is_file():
        return None
    try:
        root = ET.parse(p).getroot()
    except (OSError, ET.ParseError) as e:
        logger.debug("解析 tilemapresource.xml 失败 %s:%s", p, e)
        return None
    bb = root.find(".//BoundingBox")
    if bb is None:
        return None
    try:
        return ([float(bb.get("minx")), float(bb.get("miny")),
                 float(bb.get("maxx")), float(bb.get("maxy"))], True)
    except (TypeError, ValueError):
        return None


def bounds_from_layer_json(layer_json: Path) -> list[float] | None:
    """Cesium 地形 layer.json 的范围。

    **不要用 bounds**：实测本工具切出的地形 bounds 恒为 [-180,-90,180,90]
    （写的是世界范围，不是数据范围）。真正描述位置的是 available 的逐级
    瓦片区间，取最高级用地形网格反算。
    """
    if not layer_json.is_file():
        return None
    try:
        d = json.loads(layer_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("读 layer.json 失败 %s:%s", layer_json, e)
        return None
    available = d.get("available") or []
    for L in range(len(available) - 1, -1, -1):
        ranges = available[L]
        if not ranges:
            continue
        r = ranges[0]
        try:
            x_min, y_min = int(r["startX"]), int(r["startY"])
            x_max, y_max = int(r["endX"]), int(r["endY"])
        except (KeyError, TypeError, ValueError):
            continue
        return list(geodetic_bounds(x_min, y_min, x_max, y_max, L))
    return None


def bounds_from_tileset(tileset_json: Path) -> list[float] | None:
    """3D Tiles tileset.json 的根包围盒，返回 WGS84 度四至。

    两种形态都支持：region 是 [west,south,east,north] 弧度；box 是
    [cx,cy,cz, x 半轴, y 半轴, z 半轴]（地心 ECEF 米制）。**实测本工具产出
    的 3D Tiles 用 box**，不是 region。
    """
    if not tileset_json.is_file():
        return None
    try:
        d = json.loads(tileset_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("读 tileset.json 失败 %s:%s", tileset_json, e)
        return None
    bv = (d.get("root") or {}).get("boundingVolume") or {}

    region = bv.get("region")
    if isinstance(region, list) and len(region) == 4:
        try:
            return [math.degrees(float(v)) for v in region]
        except (TypeError, ValueError):
            return None

    box = bv.get("box")
    if not (isinstance(box, list) and len(box) == 12):
        return None
    try:
        cx, cy, cz = (float(v) for v in box[0:3])
        axes = [[float(v) for v in box[3:6]],
                [float(v) for v in box[6:9]],
                [float(v) for v in box[9:12]]]
    except (TypeError, ValueError):
        return None
    # 八个角点：中心 ± 各半轴
    corners = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                pt = [cx, cy, cz]
                for sign, axis in ((sx, axes[0]), (sy, axes[1]), (sz, axes[2])):
                    for i in range(3):
                        pt[i] += sign * axis[i]
                corners.append(tuple(pt))
    try:
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:4978", "EPSG:4326", always_xy=True)
        lons, lats = [], []
        for x, y, z in corners:
            lon, lat, _ = tr.transform(x, y, z)
            lons.append(lon)
            lats.append(lat)
    except Exception as e:
        logger.debug("3D Tiles box 转 4326 失败 %s:%s", tileset_json, e)
        return None
    return [min(lons), min(lats), max(lons), max(lats)]


def bounds_for_vector(path: Path) -> list[float] | None:
    """矢量文件的范围（解析几何）。复用既有的 inspect_vector。

    **必须实现**：现有 core/overlay.py 的矢量项不带 bounds_wgs84，靠任务 bbox
    兜底；服务图层没有任务，不解析就永远无法定位。
    """
    try:
        from .local_vector_file import inspect_vector
        info = inspect_vector(path)
    except Exception as e:
        logger.debug("解析矢量范围失败 %s:%s", path, e)
        return None
    b = info.get("bounds_wgs84")
    if isinstance(b, list) and len(b) == 4:
        return [float(v) for v in b]
    return None
