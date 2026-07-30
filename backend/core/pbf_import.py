"""从全国 osm.pbf 离线导入建筑轮廓到网格缓存。

为什么值得做:公共 Overpass 实例限流严重(实测单块请求常吃 504、要等约 38 秒
才回退),而全国 pbf 只有 1.46 GB、下载 84 秒。一次导入之后所有城市永久零请求。

**内存策略(本模块最关键的设计)**:
全国有 437 万栋建筑,全部堆在内存里按格子分组会占数 GB,再叠加节点坐标索引
(数 GB)必然爆掉。而 pbf 是按要素 ID 而非空间顺序排列的,同一个格子会在整个
扫描过程中陆续收到要素,没法边扫边把某格定稿。

故采用**外部分桶**两阶段:
  阶段1 扫 pbf,按 1° 粗桶把要素追加写到临时文件(顺序写,内存只放写缓冲)
  阶段2 逐个粗桶读回,细分成 0.05° 格子写入缓存,处理完即删该桶
内存占用被限制在"一个粗桶"的量级,临时磁盘占用随阶段2推进逐步释放。
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ..config import settings
from ..config import settings as _settings
from .building_cache import Cell, BuildingCache, cell_of
from .logs import logger

#: 粗桶边长(度)。1° 桶在密集城区约十万栋量级,单桶可安全载入内存。
BUCKET_DEG = 1.0

#: 每个粗桶的写缓冲行数,攒满才落盘(减少小写次数)
BUCKET_FLUSH_LINES = 2000

#: 建筑最少顶点数(闭合环去重后)
MIN_RING_POINTS = 3


def resolve_pbf(pbf_path=None) -> Path:
    """定位要读取的本地 pbf 文件并做基本校验。

    只认本地文件,不联网下载:pbf 由使用者自行准备(下载 Geofabrik 的
    china-latest.osm.pbf 之类),路径在 config.yaml 的 buildings.pbf_path 配置,
    或用 --pbf 参数临时指定。它只在"准备数据"时用到一次,不值得为它做
    下载/断点续传那套东西。
    """
    p = Path(pbf_path) if pbf_path else _settings.abs_path(
        getattr(_settings.buildings, "pbf_path", "./data/pbf/china.osm.pbf"))
    if not p.exists():
        raise RuntimeError(
            f"找不到 pbf 文件:{p}\n"
            "请先自行下载全国 osm.pbf(如 https://download.geofabrik.de/asia/"
            "china-latest.osm.pbf),放到该路径,\n"
            "或在 config.yaml 的 buildings.pbf_path 指定实际位置,"
            "也可用 --pbf <路径> 临时指定。")
    if p.stat().st_size < 1024:
        raise RuntimeError(f"pbf 文件过小,可能未下载完整:{p}({p.stat().st_size} 字节)")
    # 校验 pbf 头:前 4 字节是 BlobHeader 长度,紧随其后应出现 OSMHeader
    try:
        with p.open("rb") as f:
            head = f.read(48)
        n = int.from_bytes(head[:4], "big")
        if not (0 < n < 65536 and b"OSMHeader" in head):
            raise RuntimeError(f"不是有效的 osm.pbf 文件:{p}")
    except OSError as e:
        raise RuntimeError(f"读取 pbf 失败:{p}({e})") from e
    logger.info("使用本地 pbf:%s(%.2f GB)", p, p.stat().st_size / 2 ** 30)
    return p


def _tmp_dir() -> Path:
    d = settings.abs_path("./data/tmp/pbf_buckets")
    d.mkdir(parents=True, exist_ok=True)
    return d


class _BucketWriter:
    """按 1° 粗桶顺序追加要素(带写缓冲)。"""

    def __init__(self, root: Path):
        self.root = root
        self._buf: dict[tuple[int, int], list[str]] = {}
        self.lines = 0

    @staticmethod
    def bucket_of(lon: float, lat: float) -> tuple[int, int]:
        import math
        return (math.floor(lat / BUCKET_DEG), math.floor(lon / BUCKET_DEG))

    def _path(self, b: tuple[int, int]) -> Path:
        return self.root / f"b_{b[0]}_{b[1]}.jsonl"

    def add(self, lon: float, lat: float, payload: dict) -> None:
        b = self.bucket_of(lon, lat)
        buf = self._buf.setdefault(b, [])
        buf.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        self.lines += 1
        if len(buf) >= BUCKET_FLUSH_LINES:
            self._flush_one(b)

    def _flush_one(self, b: tuple[int, int]) -> None:
        buf = self._buf.get(b)
        if not buf:
            return
        with self._path(b).open("a", encoding="utf-8") as f:
            f.write("\n".join(buf) + "\n")
        buf.clear()

    def flush_all(self) -> None:
        for b in list(self._buf):
            self._flush_one(b)
        self._buf.clear()

    def buckets(self) -> list[Path]:
        return sorted(self.root.glob("b_*.jsonl"))


def _area_to_payload(area) -> dict | None:
    """osmium Area → 缓存用的 GeoJSON Feature(含内环/洞)。

    with_areas() 把闭合 way 与 multipolygon relation 统一产出为 Area,
    所以口字楼、带内院的建筑都能正确带上洞。
    一个 Area 可能有多个外环(多体建筑),取面积最大的那个——上层
    BuildingFeature 约定 rings[0] 为单一外环。
    """
    best = None          # (面积, 外环, [内环...])
    for outer in area.outer_rings():
        ring = _ring_points(outer)
        if len(ring) < MIN_RING_POINTS:
            continue
        holes = []
        for inner in area.inner_rings(outer):
            h = _ring_points(inner)
            if len(h) >= MIN_RING_POINTS:
                holes.append(h)
        a = _abs_area(ring)
        if best is None or a > best[0]:
            best = (a, ring, holes)
    if best is None:
        return None

    _a, ring, holes = best
    tags = area.tags
    # 只留建模需要的标签,不整份塞进缓存(437 万栋的体积差别很可观)
    # 高度/层数在此就解析成数值:OSM 标签是字符串("24.5 m"、"3;5"、"82'"),
    # 直接存原样会让读取侧拿 str 去和数字比较(曾因此在建模阶段抛 TypeError),
    # 且数值比字符串省体积。读取侧仍兼容字符串,以便复用历史数据。
    from ..providers.overpass import _parse_floors, _parse_height
    props = {
        # Area id 与源要素 id 不同:from_way 时 id=way_id*2,relation 时 id=rel_id*2+1。
        # 统一记成 area/<id>,仅用于去重,不需要还原成原始 osm id。
        "_fid": f"area/{area.id}",
        "_h": _parse_height(tags),
        "_fl": _parse_floors(tags),
        "_name": tags.get("name") or "",
    }
    coords = [[[round(x, 7), round(y, 7)] for x, y in ring + [ring[0]]]]
    for h in holes:
        coords.append([[round(x, 7), round(y, 7)] for x, y in h + [h[0]]])
    return {
        "type": "Feature",
        "properties": {k: v for k, v in props.items() if v not in (None, "")},
        "geometry": {"type": "Polygon", "coordinates": coords},
    }


def _ring_points(ring) -> list[tuple[float, float]]:
    """osmium ring → 去闭合重复末点的坐标列表。"""
    pts: list[tuple[float, float]] = []
    for n in ring:
        loc = n.location
        if not loc.valid():
            continue
        p = (loc.lon, loc.lat)
        if not pts or abs(p[0] - pts[-1][0]) > 1e-12 or abs(p[1] - pts[-1][1]) > 1e-12:
            pts.append(p)
    if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-12 \
            and abs(pts[0][1] - pts[-1][1]) < 1e-12:
        pts.pop()
    return pts


def _abs_area(ring) -> float:
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _centroid(ring) -> tuple[float, float]:
    n = len(ring)
    return (sum(p[0] for p in ring) / n, sum(p[1] for p in ring) / n)


def import_from_pbf(
    bbox=None,
    *,
    pbf_path: str | Path | None = None,
    cache_key: str = "osm",
    ttl_days: float = 0.0,
    on_progress=None,
    should_stop=None,
) -> dict:
    """把 pbf 里的建筑轮廓导入网格缓存。

    bbox=None 表示全国全量导入;给了 bbox 则只导入该范围(按建筑中心点判定)。
    ttl_days=0 表示导入的数据永不过期(pbf 是快照,不该按天过期)。

    返回统计 dict。可中断:中断时已写入的格子保留,但不登记覆盖清单
    (避免把不完整的导入当成"这片已全覆盖")。
    """
    import osmium
    import osmium.filter as of

    pbf = resolve_pbf(pbf_path)

    tmp = _tmp_dir()
    shutil.rmtree(tmp, ignore_errors=True)      # 上次残留的桶不能混进来
    tmp.mkdir(parents=True, exist_ok=True)
    writer = _BucketWriter(tmp)

    stats = {
        "pbf": str(pbf),
        "bbox": list(bbox) if bbox else None,
        "areas_seen": 0,
        "buildings": 0,
        "skipped_outside": 0,
        "skipped_bad_geom": 0,
        "cells": 0,
        "bytes": 0,
        "scan_seconds": 0.0,
        "group_seconds": 0.0,
    }

    # ---------- 阶段 1:扫 pbf,按 1° 粗桶落临时文件 ----------
    logger.info("pbf 导入 阶段1/2:扫描建筑要素%s",
                f",限定范围 {bbox}" if bbox else ",全国范围")
    t0 = time.time()
    last_log = t0
    # 实际数据范围(minx,miny,maxx,maxy),用于登记覆盖范围
    data_extent = [1e18, 1e18, -1e18, -1e18]
    # 只用 with_areas():它内部自行维护节点坐标索引(node_location_storage 是
    # 只读属性)。**不要再叠加 with_locations()**——两个节点索引处理器同时挂上
    # 会让遍历卡死(实测在 789 字节的测试 pbf 上都跑不完)。
    fp = (osmium.FileProcessor(pbf)
          .with_areas()
          .with_filter(of.EntityFilter(osmium.osm.AREA))
          .with_filter(of.KeyFilter("building")))

    for area in fp:
        if should_stop and should_stop():
            writer.flush_all()
            logger.info("pbf 导入被中断(阶段1,已收集 %d 栋)", stats["buildings"])
            shutil.rmtree(tmp, ignore_errors=True)
            stats["interrupted"] = True
            return stats
        stats["areas_seen"] += 1
        payload = _area_to_payload(area)
        if payload is None:
            stats["skipped_bad_geom"] += 1
            continue
        ring = payload["geometry"]["coordinates"][0]
        cx, cy = _centroid(ring)
        if bbox is not None:
            if not (bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]):
                stats["skipped_outside"] += 1
                continue
        writer.add(cx, cy, payload)
        stats["buildings"] += 1
        if cx < data_extent[0]: data_extent[0] = cx
        if cy < data_extent[1]: data_extent[1] = cy
        if cx > data_extent[2]: data_extent[2] = cx
        if cy > data_extent[3]: data_extent[3] = cy

        now = time.time()
        if now - last_log >= 10.0:
            last_log = now
            logger.info("pbf 导入 阶段1:已收集 %d 栋(扫过 %d 个面),%.0fs",
                        stats["buildings"], stats["areas_seen"], now - t0)
            if on_progress:
                on_progress("scan", stats["buildings"], None)

    writer.flush_all()
    stats["scan_seconds"] = time.time() - t0
    logger.info("pbf 导入 阶段1 完成:%d 栋建筑,%d 个粗桶,用时 %.0fs",
                stats["buildings"], len(writer.buckets()), stats["scan_seconds"])

    # ---------- 阶段 2:逐粗桶细分成 0.05° 格写入缓存 ----------
    cache = BuildingCache(cache_key, ttl_days=ttl_days)
    buckets = writer.buckets()
    t1 = time.time()
    logger.info("pbf 导入 阶段2/2:按 %.2f° 网格写入缓存(%d 个粗桶)",
                0.05, len(buckets))

    for i, bp in enumerate(buckets, start=1):
        if should_stop and should_stop():
            logger.info("pbf 导入被中断(阶段2,已写 %d 格)", stats["cells"])
            stats["interrupted"] = True
            return stats
        groups: dict[tuple[int, int], list[dict]] = {}
        with bp.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    feat = json.loads(line)
                except ValueError:
                    continue
                ring = (feat.get("geometry") or {}).get("coordinates", [[]])[0]
                if not ring:
                    continue
                cx, cy = _centroid(ring)
                c = cell_of(cx, cy)
                groups.setdefault((c.row, c.col), []).append(feat)
        for (r, col), feats in groups.items():
            # 不写逐格 meta:批量导入的新鲜度由覆盖清单统一判定
            cache.write(Cell(r, col), feats, source="pbf", write_meta=False)
            stats["cells"] += 1
        bp.unlink(missing_ok=True)      # 处理完即删,临时占用随进度释放
        if on_progress:
            on_progress("group", i, len(buckets))
        if i % 20 == 0 or i == len(buckets):
            logger.info("pbf 导入 阶段2:%d/%d 桶,已写 %d 格", i, len(buckets), stats["cells"])

    stats["group_seconds"] = time.time() - t1
    shutil.rmtree(tmp, ignore_errors=True)

    # 登记覆盖范围:让"这片没有建筑的空格子"也算已知,不再联网确认。
    # 只在完整跑完后登记——中断时登记会把不完整导入当成全覆盖。
    #
    # 未指定 bbox 时**不能登记全球**:pbf 通常只含某个国家/地区,登记成全球会让
    # 范围外(如国外)的查询命中空缓存、静默返回 0 栋建筑。改用扫描过程中统计到
    # 的实际数据范围,对任何 pbf 都自适应。
    if bbox is not None:
        cov_bbox = tuple(bbox)
    elif data_extent[0] < 1e17:
        # 数据范围外扩一格,避免边缘格子因"未完全包含"而落空
        from .building_cache import CELL_DEG
        cov_bbox = (data_extent[0] - CELL_DEG, data_extent[1] - CELL_DEG,
                    data_extent[2] + CELL_DEG, data_extent[3] + CELL_DEG)
    else:
        cov_bbox = None
    if cov_bbox:
        cache.add_coverage(cov_bbox, source="pbf", buildings=stats["buildings"])
        stats["coverage_bbox"] = [round(v, 4) for v in cov_bbox]
        logger.info("已登记覆盖范围:%s(范围外仍会联网抓取)", stats["coverage_bbox"])

    st = cache.stats()
    stats["cells_total"] = st["cells"]
    stats["bytes"] = st["bytes"]
    logger.info("pbf 导入完成:%d 栋 → %d 格,缓存共 %.1f MB,阶段1 %.0fs / 阶段2 %.0fs",
                stats["buildings"], stats["cells"], st["bytes"] / 1048576,
                stats["scan_seconds"], stats["group_seconds"])
    return stats
