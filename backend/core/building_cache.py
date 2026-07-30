"""建筑轮廓的网格化本地缓存。

为什么按**固定网格**缓存而不是按"某次下载的范围"缓存:

如果记录"上次下载了范围 R",下次遇到范围 R' 就只能比较两者的重叠率,而按
重叠率阈值复用是**不正确**的——重叠 85% 就用缓存,意味着剩下 15% 区域完全
没有数据,那片的建筑会静默缺失,成果看着完整实际少一块。

改成固定网格后,"部分重叠"的语义自然变成"复用已有格子、只补缺失格子":
既没有正确性风险,又比阈值方案更省(85% 重叠时只请求那 15%)。这与本项目
栅格瓦片缓存 data/tiles/{provider}/{z}/{col}_{row}.png 是同一个思路。

**格子必须全局对齐**(而非相对用户框选的范围划分),否则格子边界随每次下载
浮动,永远无法复用。
"""
from __future__ import annotations

import gzip
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from ..config import settings
from .logs import logger

#: 格子边长(度)。与 Overpass 的分块尺寸一致,便于"缺一格 = 发一次请求"。
CELL_DEG = 0.05

#: 缓存有效期(天)。OSM 建筑变化慢,30 天足够;超期视为缺失、重新抓取。
DEFAULT_TTL_DAYS = 30

#: 覆盖范围包含判定的容差(度)。约 0.1 毫米,远小于任何有意义的位置差异,
#: 但足以吸收格边界计算的浮点误差(见 _covered 注释)。
_COV_EPS = 1e-9


def cache_root() -> Path:
    d = settings.abs_path("./data/buildings")
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass(frozen=True)
class Cell:
    """一个缓存格子。row/col 为全局对齐的整数索引。"""
    row: int
    col: int

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """该格的 (west, south, east, north)。"""
        west = self.col * CELL_DEG - 180.0
        south = self.row * CELL_DEG - 90.0
        return (west, south, west + CELL_DEG, south + CELL_DEG)

    def __str__(self) -> str:
        return f"{self.row}_{self.col}"


#: 格线吸附容差(以格为单位)。坐标落在格线上时,(114.40+180)/0.05 会算出
#: 5887.999999999999,floor 得 5887——凭空多出左边一整列格子(每格一次网络
#: 请求)。1e-7 格 ≈ 5e-9 度 ≈ 亚毫米,远小于任何有意义的位置差异。
_EPS = 1e-7


def cell_of(lon: float, lat: float) -> Cell:
    """点所在的格子。"""
    return Cell(row=math.floor((lat + 90.0) / CELL_DEG + _EPS),
                col=math.floor((lon + 180.0) / CELL_DEG + _EPS))


def cells_for_bbox(bbox) -> list[Cell]:
    """覆盖 bbox 的全部格子(含边界)。

    两侧都做格线吸附:左/下边界正好在格线上时不多算前一格,
    右/上边界正好在格线上时不多算后一格。否则每次下载都会白请求一整圈格子。
    """
    west, south, east, north = bbox
    c0 = math.floor((west + 180.0) / CELL_DEG + _EPS)
    r0 = math.floor((south + 90.0) / CELL_DEG + _EPS)
    c1 = math.ceil((east + 180.0) / CELL_DEG - _EPS) - 1
    r1 = math.ceil((north + 90.0) / CELL_DEG - _EPS) - 1
    c1 = max(c1, c0)
    r1 = max(r1, r0)
    return [Cell(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


class BuildingCache:
    """按格子存取建筑要素。

    每格两个文件:
      {row}_{col}.geojson.gz   要素集合(GeoJSON FeatureCollection,gzip)
      {row}_{col}.meta.json    抓取时间 / 要素数 / 数据源
    meta 单独存是为了判断新鲜度时不必解压整个 gz。
    """

    def __init__(self, source_key: str = "osm", ttl_days: float = DEFAULT_TTL_DAYS):
        self.source_key = source_key
        self.ttl_seconds = float(ttl_days) * 86400.0
        self.dir = cache_root() / source_key
        self.dir.mkdir(parents=True, exist_ok=True)
        self._coverage: list[dict] | None = None      # 懒加载

    # ---------- 区域覆盖清单 ----------
    # pbf 批量导入会一次覆盖一大片区域,其中**没有建筑的格子**同样属于"已知",
    # 不该再去联网请求。但为几十万个空格子各写一个文件不可行,故用一份区域
    # 清单记录"这片范围已由 pbf 导入过",查询时按范围判定,空格子自然命中。

    @property
    def _coverage_path(self) -> Path:
        return self.dir / "_coverage.json"

    def coverage(self) -> list[dict]:
        if self._coverage is None:
            try:
                self._coverage = json.loads(
                    self._coverage_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._coverage = []
        return self._coverage

    def add_coverage(self, bbox, *, source: str = "pbf", buildings: int = 0,
                     dedup: bool = False) -> None:
        """登记一片已批量导入的区域。

        dedup=True:若已有未过期记录完全包含该范围则跳过。远端按需取包时
        每个任务都会重新登记同一批包,不去重的话清单会无限膨胀。
        """
        cov = self.coverage()
        if dedup:
            now = time.time()
            w, s, e, n = bbox
            for rec in cov:
                if self.ttl_seconds > 0 and \
                        (now - float(rec.get("imported_at") or 0)) > self.ttl_seconds:
                    continue
                bb = rec.get("bbox") or []
                if len(bb) == 4 and bb[0] <= w + _COV_EPS and bb[1] <= s + _COV_EPS \
                        and bb[2] >= e - _COV_EPS and bb[3] >= n - _COV_EPS:
                    return
        cov.append({
            "bbox": [float(v) for v in bbox],
            "imported_at": time.time(),
            "source": source,
            "buildings": int(buildings),
        })
        self._coverage = cov
        try:
            self._coverage_path.write_text(
                json.dumps(cov, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            logger.warning("写覆盖清单失败:%s", str(e)[:120])

    def clear_coverage(self) -> None:
        self._coverage = []
        self._coverage_path.unlink(missing_ok=True)

    def _covered(self, cell: Cell) -> bool:
        """格子是否落在某条未过期的覆盖记录内(要求格子完全包含在内)。"""
        now = time.time()
        w, s, e, n = cell.bounds
        for rec in self.coverage():
            if self.ttl_seconds > 0:
                if (now - float(rec.get("imported_at") or 0)) > self.ttl_seconds:
                    continue
            bb = rec.get("bbox") or []
            if len(bb) != 4:
                continue
            # 整格都在导入范围内才算覆盖:部分相交时格子边缘可能缺数据。
            # 必须带容差:格边界由 row*0.05-90 算出,会得到 30.500000000000045
            # 这类值,与覆盖记录里的 30.5 比较时差 4.5e-14 就判不包含,
            # 导致明明已覆盖的格子被判为缺失、每次都回落联网。
            if (bb[0] <= w + _COV_EPS and bb[1] <= s + _COV_EPS
                    and bb[2] >= e - _COV_EPS and bb[3] >= n - _COV_EPS):
                return True
        return False

    # ---------- 路径 ----------

    def _data_path(self, cell: Cell) -> Path:
        return self.dir / f"{cell}.geojson.gz"

    def _meta_path(self, cell: Cell) -> Path:
        return self.dir / f"{cell}.meta.json"

    # ---------- 新鲜度 ----------

    def meta_of(self, cell: Cell) -> dict | None:
        p = self._meta_path(cell)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def is_fresh(self, cell: Cell) -> bool:
        """格子是否已有可用数据。

        两种命中方式:
          1. 该格自己的缓存文件存在且未超期
          2. 该格完全落在某片未过期的批量导入区域内(pbf 导入)——
             此时即使没有文件也算命中,因为"这片没有建筑"本身就是已知结论,
             不该再联网确认一次。
        """
        meta = self.meta_of(cell)
        if not meta:
            return self._covered(cell)
        data = self._data_path(cell)
        if not (data.exists() and data.stat().st_size > 0):
            return self._covered(cell)
        ts = float(meta.get("fetched_at") or 0.0)
        if self.ttl_seconds <= 0:
            return True          # ttl<=0 表示永不过期
        return (time.time() - ts) <= self.ttl_seconds

    def split(self, cells: list[Cell]) -> tuple[list[Cell], list[Cell]]:
        """把格子分成 (命中, 需抓取) 两组。"""
        hit, miss = [], []
        for c in cells:
            (hit if self.is_fresh(c) else miss).append(c)
        return hit, miss

    # ---------- 读写 ----------

    def read(self, cell: Cell) -> list[dict]:
        """读回该格的 GeoJSON features。

        文件不存在但落在批量导入区域内 → 返回空列表(该格确实没有建筑),
        不视为异常。损坏则当空并清理。
        """
        p = self._data_path(cell)
        if not p.exists():
            return []
        try:
            with gzip.open(p, "rt", encoding="utf-8") as f:
                gj = json.load(f)
            return gj.get("features") or []
        except (OSError, ValueError) as e:
            logger.warning("建筑缓存格 %s 读取失败,将重新抓取:%s", cell, str(e)[:120])
            self.invalidate(cell)
            return []

    def write(self, cell: Cell, features: list[dict], *, source: str = "",
              write_meta: bool = True) -> None:
        """写入该格。先写临时文件再改名,避免中断留下半个 gz 被当成有效缓存。

        write_meta=False 用于批量导入(pbf):那时新鲜度由覆盖清单统一判定,
        逐格再写一个 meta 文件纯属冗余——7 万格会多出 7 万个几百字节的小文件,
        按 4KB 簇计算白占近 200MB,还让 stats() 要遍历双倍文件数。
        """
        data_p = self._data_path(cell)
        tmp = data_p.with_suffix(data_p.suffix + ".tmp")
        gj = {"type": "FeatureCollection", "features": features}
        try:
            with gzip.open(tmp, "wt", encoding="utf-8", compresslevel=6) as f:
                json.dump(gj, f, ensure_ascii=False)
            tmp.replace(data_p)
            if not write_meta:
                return
            self._meta_path(cell).write_text(json.dumps({
                "fetched_at": time.time(),
                "feature_count": len(features),
                "source": source or self.source_key,
                "cell": str(cell),
                "bounds": list(cell.bounds),
                "cell_deg": CELL_DEG,
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError as e:
            logger.warning("建筑缓存格 %s 写入失败(不影响本次成果):%s", cell, str(e)[:120])
            tmp.unlink(missing_ok=True)

    def invalidate(self, cell: Cell) -> None:
        self._data_path(cell).unlink(missing_ok=True)
        self._meta_path(cell).unlink(missing_ok=True)

    # ---------- 统计与清理 ----------

    def stats(self) -> dict:
        """缓存占用与格子数(供界面展示/清理)。"""
        n = 0
        total = 0
        fresh = 0
        now = time.time()
        for p in self.dir.glob("*.geojson.gz"):
            try:
                total += p.stat().st_size
            except OSError:
                continue
            n += 1
            meta_p = p.with_name(p.name.replace(".geojson.gz", ".meta.json"))
            try:
                ts = float(json.loads(meta_p.read_text(encoding="utf-8")).get("fetched_at") or 0)
                if self.ttl_seconds <= 0 or (now - ts) <= self.ttl_seconds:
                    fresh += 1
                continue
            except (OSError, ValueError):
                pass
            # 无 meta(批量导入的格):按覆盖清单判定
            try:
                r, c = p.name.split(".")[0].split("_")
                if self._covered(Cell(int(r), int(c))):
                    fresh += 1
            except (ValueError, IndexError):
                pass
        return {
            "source": self.source_key,
            "cells": n,
            "fresh_cells": fresh,
            "bytes": total,
            "cell_deg": CELL_DEG,
            "ttl_days": self.ttl_seconds / 86400.0,
        }

    def clear(self, bbox=None) -> int:
        """清理缓存。给 bbox 只清该范围覆盖的格子,否则全清。返回清理格数。"""
        if bbox is None:
            n = 0
            for p in list(self.dir.glob("*.geojson.gz")) + list(self.dir.glob("*.meta.json")):
                try:
                    p.unlink(missing_ok=True)
                    if p.name.endswith(".geojson.gz"):
                        n += 1
                except OSError:
                    pass
            logger.info("已清空建筑缓存(%s):%d 格", self.source_key, n)
            return n
        n = 0
        for c in cells_for_bbox(bbox):
            if self._data_path(c).exists():
                self.invalidate(c)
                n += 1
        logger.info("已清理建筑缓存(%s)范围内 %d 格", self.source_key, n)
        return n


def feature_to_geojson(feat) -> dict:
    """BuildingFeature → GeoJSON Feature(用于写入缓存)。

    原始属性(height/num_floors/name/props)一并存下,这样缓存与"用哪种高度
    补全策略"无关:改了兜底高度或层高只需重跑建模,不必重新联网抓数据。
    注意存的是**原始** height(可能为 None),不是清洗后补全的值。
    """
    ring_list = [list(r) + [r[0]] for r in feat.rings]      # 写盘时闭合
    props = {
        "_fid": feat.fid,
        "_h": feat.height,
        "_fl": feat.num_floors,
        "_name": feat.name or "",
    }
    if feat.props:
        props["_p"] = feat.props
    return {
        "type": "Feature",
        "properties": props,
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[float(x), float(y)] for x, y in ring] for ring in ring_list],
        },
    }


def geojson_to_feature(gf: dict):
    """GeoJSON Feature → BuildingFeature(从缓存读回)。不可用返回 None。

    高度/层数一律在此归一化为数值:缓存里可能是 OSM 原始标签字符串
    ("24.5" / "24.5 m" / "8" / "3;5"),pbf 导入写的就是原样字符串。
    不在这里转的话,上层 `num_floors > 0` 会拿 str 和 int 比较直接抛
    TypeError。这里是所有缓存读取的唯一出口,放在此处能同时兼容
    历史数据与线上数据包,不必重新生成。
    """
    from ..providers.buildings import BuildingFeature
    from ..providers.overpass import _parse_height, _parse_floors

    geom = gf.get("geometry") or {}
    if geom.get("type") != "Polygon":
        return None
    rings = []
    for ring in geom.get("coordinates") or []:
        coords = [(float(c[0]), float(c[1])) for c in ring or [] if c and len(c) >= 2]
        # 去掉写盘时补的闭合点
        if len(coords) >= 2 and abs(coords[0][0] - coords[-1][0]) < 1e-12 \
                and abs(coords[0][1] - coords[-1][1]) < 1e-12:
            coords = coords[:-1]
        if len(coords) >= 3:
            rings.append(coords)
    if not rings:
        return None
    p = gf.get("properties") or {}
    # 复用 Overpass 那套标签解析:能处理 "24.5 m"、英尺、"3;5" 多值等写法
    h = p.get("_h")
    fl = p.get("_fl")
    return BuildingFeature(
        fid=str(p.get("_fid") or ""),
        rings=rings,
        height=(float(h) if isinstance(h, (int, float))
                else _parse_height({"height": h}) if h is not None else None),
        num_floors=(int(fl) if isinstance(fl, int)
                    else _parse_floors({"building:levels": fl}) if fl is not None else None),
        name=p.get("_name") or "",
        props=dict(p.get("_p") or {}),
    )


def feature_key(feat: dict) -> str:
    """要素去重键:优先用 OSM 的 type/id,回退几何首点。

    跨格边界的建筑会在相邻格各存一份,合并时必须去重。
    """
    props = feat.get("properties") or {}
    fid = props.get("_fid") or feat.get("id")
    if fid:
        return str(fid)
    geom = feat.get("geometry") or {}
    coords = geom.get("coordinates")
    try:
        p = coords
        while isinstance(p, list) and p and isinstance(p[0], list):
            p = p[0]
        return f"geom:{p[0]:.7f},{p[1]:.7f}"
    except Exception:
        return repr(coords)[:64]
