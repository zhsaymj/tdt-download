"""把本地格子缓存打包成可上传到对象存储(COS)的数据包 / 从远端按需取回。

目的:全国 pbf 导入要 33 分钟 + 1.46GB 下载,不该每台机器都做一遍。
准备一次上传到 COS,各机运行时按当前下载范围只拉需要的包(几十 KB 起)。

**为何打包而不是直接传格子文件**:实测格子中位大小仅 0.4 KB,7 万个这种小对象
走 HTTP 时请求开销完全盖过传输本身,上传也极慢。按 0.5° 聚合成约 3680 个包后
中位 8.8 KB、90 分位 97 KB,一个城区范围通常一次请求即可。

产物(data/buildings/dist/<source>/,整个目录上传到 COS):
  index.json               清单:网格参数、覆盖范围、包列表、版本号
  b_{brow}_{bcol}.json.gz  一个 0.5° 包,内含该范围各格子的要素

取回:见 remote_bundles.py
"""
from __future__ import annotations

import gzip
import json
import math
import time
from pathlib import Path

from ..config import settings
from .building_cache import CELL_DEG, Cell, cache_root
from .logs import logger

#: 包边长(度)。0.5° 为实测折中:对象数约 3680 便于上传,
#: 中位 8.8 KB,小范围下载不会过度获取。
BUNDLE_DEG = 0.5

#: 格线吸附容差(同 building_cache,避免边界坐标因浮点误差多算一整列包)
_EPS = 1e-7


def dist_root() -> Path:
    d = settings.abs_path("./data/buildings/dist")
    d.mkdir(parents=True, exist_ok=True)
    return d


def bundle_of(cell: Cell, bundle_deg: float = BUNDLE_DEG) -> tuple[int, int]:
    """格子所属包索引(与格子同为全局对齐)。"""
    lat = cell.row * CELL_DEG - 90.0
    lon = cell.col * CELL_DEG - 180.0
    return (math.floor((lat + 90.0) / bundle_deg + _EPS),
            math.floor((lon + 180.0) / bundle_deg + _EPS))


def bundles_for_bbox(bbox, bundle_deg: float = BUNDLE_DEG) -> list[tuple[int, int]]:
    """覆盖 bbox 的包索引列表。"""
    west, south, east, north = bbox
    c0 = math.floor((west + 180.0) / bundle_deg + _EPS)
    r0 = math.floor((south + 90.0) / bundle_deg + _EPS)
    c1 = max(math.ceil((east + 180.0) / bundle_deg - _EPS) - 1, c0)
    r1 = max(math.ceil((north + 90.0) / bundle_deg - _EPS) - 1, r0)
    return [(r, c) for r in range(r0, r1 + 1) for c in range(c0, c1 + 1)]


def bundle_name(b) -> str:
    return f"b_{b[0]}_{b[1]}.json.gz"


def bundle_bounds(b, bundle_deg: float = BUNDLE_DEG):
    """包的地理范围 (west, south, east, north)。

    取回一个包 = 该范围内数据已齐(含"哪些格子是空的"这个结论),
    故它是登记缓存覆盖范围的正确单位——比请求 bbox 合适:
    请求范围可能远小于一个格子,用它登记会导致格子永远无法判定为已覆盖。
    """
    west = b[1] * bundle_deg - 180.0
    south = b[0] * bundle_deg - 90.0
    return (west, south, west + bundle_deg, south + bundle_deg)


def pack(source_key: str = "osm", *, bundle_deg: float = BUNDLE_DEG,
         on_progress=None, should_stop=None) -> dict:
    """把本地格子缓存打包到 data/buildings/dist/<source_key>/,返回清单摘要。"""
    from .building_cache import BuildingCache

    src_dir = cache_root() / source_key
    if not src_dir.is_dir():
        raise RuntimeError(f"本地缓存目录不存在:{src_dir}(请先导入数据)")

    out_dir = dist_root() / source_key
    out_dir.mkdir(parents=True, exist_ok=True)
    for p in out_dir.glob("b_*.json.gz"):
        p.unlink(missing_ok=True)      # 清旧包,避免残留混进新清单

    groups: dict[tuple[int, int], list[Cell]] = {}
    for p in src_dir.glob("*.geojson.gz"):
        try:
            r, c = p.name.split(".")[0].split("_")
            cell = Cell(int(r), int(c))
        except ValueError:
            continue
        groups.setdefault(bundle_of(cell, bundle_deg), []).append(cell)
    if not groups:
        raise RuntimeError(f"{src_dir} 下没有可打包的格子")

    logger.info("开始打包:%d 格 → %d 个 %.2f° 包",
                sum(len(v) for v in groups.values()), len(groups), bundle_deg)

    cache = BuildingCache(source_key, ttl_days=0)
    bundles: list[dict] = []
    total_bytes = total_feats = 0
    minx = miny = 1e18
    maxx = maxy = -1e18
    t0 = time.time()

    for i, (b, cells) in enumerate(sorted(groups.items()), start=1):
        if should_stop and should_stop():
            logger.info("打包被中断(已写 %d/%d)", i - 1, len(groups))
            return {"interrupted": True, "bundles": len(bundles)}
        payload: dict[str, list] = {}
        nfeat = 0
        for cell in cells:
            feats = cache.read(cell)
            if not feats:
                continue
            payload[str(cell)] = feats
            nfeat += len(feats)
            w, s, e, n = cell.bounds
            minx = min(minx, w); maxx = max(maxx, e)
            miny = min(miny, s); maxy = max(maxy, n)
        if not payload:
            continue
        path = out_dir / bundle_name(b)
        with gzip.open(path, "wt", encoding="utf-8", compresslevel=6) as f:
            json.dump({"cells": payload}, f, ensure_ascii=False)
        size = path.stat().st_size
        total_bytes += size
        total_feats += nfeat
        bundles.append({"b": [b[0], b[1]], "bytes": size,
                        "cells": len(payload), "features": nfeat})
        if on_progress and (i % 50 == 0 or i == len(groups)):
            on_progress(i, len(groups))
        if i % 500 == 0:
            logger.info("打包 %d/%d,%.1f MB", i, len(groups), total_bytes / 1048576)

    coverage = ([round(minx, 6), round(miny, 6), round(maxx, 6), round(maxy, 6)]
                if minx < 1e17 else None)
    index = {
        # 版本号:客户端据此判断远端是否更新过(变了就重取清单)
        "version": time.strftime("%Y%m%d-%H%M%S"),
        "source": source_key,
        "cell_deg": CELL_DEG,
        "bundle_deg": bundle_deg,
        # 覆盖范围必须是**真实数据范围**:客户端据此区分"这片确实没建筑"
        # 与"这片没导入过、应回落 Overpass"。之前把它当成全球导致国外
        # 查询静默返回 0 栋,是同一个坑,这里不能再犯。
        "coverage_bbox": coverage,
        "bundle_count": len(bundles),
        "total_bytes": total_bytes,
        "total_features": total_feats,
        "bundles": bundles,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    (out_dir / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    logger.info("打包完成:%d 包 / %d 栋 / %.1f MB,用时 %.0fs,覆盖 %s",
                len(bundles), total_feats, total_bytes / 1048576,
                time.time() - t0, coverage)
    return {k: v for k, v in index.items() if k != "bundles"} | {"dir": str(out_dir)}
