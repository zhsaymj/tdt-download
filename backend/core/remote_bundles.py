"""从对象存储(COS)按需取回建筑数据包,解包写入本地格子缓存。

解决的问题:全国 pbf 导入要 33 分钟 + 1.46GB 下载,不该每台机器重做一遍。
准备一次上传 COS,各机运行时只拉当前下载范围需要的包(几十 KB 起)。

取数三级回落(逐级变慢):
    本地格子缓存  →  COS 数据包  →  Overpass
    (毫秒)          (首次几十KB)    (最后兜底)

清单(index.json)只取一次并本地缓存,其中 coverage_bbox 决定:
  - 范围内且包 404  → 该片确实没建筑,不必联网确认
  - 范围外          → 远端没这块数据,回落 Overpass
"""
from __future__ import annotations

import gzip
import io
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..config import settings
from .building_cache import BuildingCache, Cell
from .bundle_pack import BUNDLE_DEG, bundle_name, bundles_for_bbox
from .logs import logger

#: 清单本地缓存有效期(秒):过期后重新取一次,以便发现远端更新
INDEX_TTL = 6 * 3600
#: 单个请求超时(秒)
HTTP_TIMEOUT = 60
#: 单包下载失败重试次数
MAX_RETRIES = 2


def _base_url() -> str:
    """远端数据包目录地址(不含结尾斜杠)。未配置返回空串。"""
    url = (getattr(settings.buildings, "remote_url", "") or "").strip()
    return url.rstrip("/")


def _opener():
    proxy = (getattr(settings.buildings, "proxy", "") or "").strip()
    handlers = []
    if proxy:
        p = proxy if "://" in proxy else f"http://{proxy}"
        handlers.append(urllib.request.ProxyHandler({"http": p, "https": p}))
    return urllib.request.build_opener(*handlers)


class RemoteBundleStore:
    """按需从远端取包并写入本地格子缓存。"""

    def __init__(self, source_key: str = "osm", cache: BuildingCache | None = None):
        self.source_key = source_key
        self.cache = cache or BuildingCache(source_key, ttl_days=0)
        self.base = f"{_base_url()}/{source_key}" if _base_url() else ""
        self._index: dict | None = None
        # 本进程内已尝试过的包,避免同一任务里对同一个包反复请求
        self._tried: set[tuple[int, int]] = set()

    @property
    def enabled(self) -> bool:
        return bool(self.base)

    # ---------- 清单 ----------

    @property
    def _index_path(self) -> Path:
        return self.cache.dir / "_remote_index.json"

    def index(self, force: bool = False) -> dict | None:
        """取清单:优先本地缓存(未过期),否则联网取回。失败返回 None。"""
        if self._index is not None and not force:
            return self._index
        p = self._index_path
        if not force and p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                if (time.time() - float(data.get("_fetched_at") or 0)) <= INDEX_TTL:
                    self._index = data
                    return data
            except (OSError, ValueError):
                pass
        if not self.enabled:
            return None
        try:
            with _opener().open(f"{self.base}/index.json", timeout=HTTP_TIMEOUT) as r:
                if r.status != 200:
                    raise RuntimeError(f"HTTP {r.status}")
                data = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            logger.warning("取远端建筑数据清单失败(将回落联网抓取):%s", str(e)[:160])
            # 联网失败时,过期的本地清单仍比没有好
            if p.exists():
                try:
                    self._index = json.loads(p.read_text(encoding="utf-8"))
                    return self._index
                except (OSError, ValueError):
                    pass
            return None
        data["_fetched_at"] = time.time()
        try:
            p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass
        self._index = data
        logger.info("远端建筑数据清单:版本 %s,%d 个包,%.1f MB,覆盖 %s",
                    data.get("version"), data.get("bundle_count", 0),
                    (data.get("total_bytes") or 0) / 1048576,
                    data.get("coverage_bbox"))
        return data

    def bundle_deg(self) -> float:
        idx = self.index()
        return float((idx or {}).get("bundle_deg") or BUNDLE_DEG)

    def coverage_bbox(self):
        idx = self.index()
        bb = (idx or {}).get("coverage_bbox")
        return tuple(bb) if bb and len(bb) == 4 else None

    def covers(self, bbox) -> bool:
        """远端数据是否覆盖该范围(部分相交即算,细节由逐包判断)。"""
        cov = self.coverage_bbox()
        if not cov:
            return False
        w, s, e, n = bbox
        return not (cov[2] < w or cov[0] > e or cov[3] < s or cov[1] > n)

    # ---------- 取包 ----------

    def _existing_bundles(self) -> set[tuple[int, int]] | None:
        """清单里声明存在的包集合(用于跳过不存在的包,省掉 404 往返)。"""
        idx = self.index()
        if not idx or "bundles" not in idx:
            return None
        try:
            return {(int(b["b"][0]), int(b["b"][1])) for b in idx["bundles"]}
        except (KeyError, TypeError, ValueError):
            return None

    def fetch_bbox(self, bbox, *, on_progress=None, should_stop=None) -> dict:
        """把该范围需要的包全部取回并写入本地缓存。

        返回 {bundles_needed, bundles_fetched, cells_written, features, bytes}
        """
        stat = {"bundles_needed": 0, "bundles_fetched": 0, "skipped_absent": 0,
                "cells_written": 0, "features": 0, "bytes": 0}
        if not self.enabled:
            return stat
        idx = self.index()
        if not idx:
            return stat

        need = bundles_for_bbox(bbox, self.bundle_deg())
        present = self._existing_bundles()
        stat["bundles_needed"] = len(need)
        done_bundles: list[tuple[int, int]] = []   # 已确认数据齐备的包

        for i, b in enumerate(need, start=1):
            if should_stop and should_stop():
                return stat
            if b in self._tried:
                continue
            self._tried.add(b)
            # 清单里没有这个包 → 该片没有建筑,不必请求。
            # 这同样是"已知结论",要登记覆盖,否则每次都会回落联网。
            if present is not None and b not in present:
                stat["skipped_absent"] += 1
                done_bundles.append(b)
                continue
            data, nbytes = self._get_bundle(b)
            if data is None:
                continue
            stat["bundles_fetched"] += 1
            stat["bytes"] += nbytes
            done_bundles.append(b)
            for key, feats in (data.get("cells") or {}).items():
                try:
                    r, c = key.split("_")
                    cell = Cell(int(r), int(c))
                except ValueError:
                    continue
                # 不写逐格 meta:新鲜度由覆盖清单统一判定
                self.cache.write(cell, feats, source="remote", write_meta=False)
                stat["cells_written"] += 1
                stat["features"] += len(feats)
            if on_progress:
                on_progress(i, len(need))

        # 登记覆盖:让这片里"没有建筑的空格"也算已知,不再联网确认。
        #
        # 按**包的范围**登记而不是请求 bbox:请求范围可能远小于一个格子
        # (如一个小区只有 0.007°),用它登记时格子永远无法满足"完全包含"
        # 判定,于是每次都回落联网——这正是最初的实现犯的错。
        # 取回一个包意味着该 0.5° 区域数据已齐(含哪些格子为空),它才是
        # 正确的登记单位。
        from .bundle_pack import bundle_bounds
        bd = self.bundle_deg()
        for b in done_bundles:
            self.cache.add_coverage(bundle_bounds(b, bd), source="remote",
                                    buildings=0, dedup=True)
        if stat["bundles_fetched"] or stat["skipped_absent"]:
            logger.info("远端数据包:需要 %d 个,取回 %d 个(清单内不存在 %d 个),"
                        "写入 %d 格 / %d 栋 / %.1f MB",
                        stat["bundles_needed"], stat["bundles_fetched"],
                        stat["skipped_absent"], stat["cells_written"],
                        stat["features"], stat["bytes"] / 1048576)
        return stat

    def _get_bundle(self, b: tuple[int, int]) -> tuple[dict | None, int]:
        """下载并解开一个包,返回 (数据, 传输字节数)。

        404 表示该包不存在 = 这片确实没有建筑,与"请求失败"一样返回 None,
        但不重试(重试没意义)。
        """
        url = f"{self.base}/{bundle_name(b)}"
        for attempt in range(MAX_RETRIES + 1):
            try:
                with _opener().open(url, timeout=HTTP_TIMEOUT) as r:
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}")
                    raw = r.read()
                with gzip.open(io.BytesIO(raw), "rt", encoding="utf-8") as f:
                    data = json.load(f)
                return data, len(raw)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    return None, 0      # 该包不存在 = 这片没有建筑
                if attempt >= MAX_RETRIES:
                    logger.warning("取包 %s 失败 HTTP %s", bundle_name(b), e.code)
                    return None, 0
            except Exception as e:
                if attempt >= MAX_RETRIES:
                    logger.warning("取包 %s 失败:%s", bundle_name(b), str(e)[:140])
                    return None, 0
                time.sleep(1 + attempt)
        return None, 0
