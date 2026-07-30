"""建筑数据源的网格缓存装饰器。

包在任意 Building3DSource 外层,把"按范围取数"变成"按格子取数 + 逐格缓存":

    范围 → 覆盖的格子集合
      ├─ 新鲜的格子 → 直接读本地
      └─ 缺失/过期的格子 → 调用内层数据源取该格 → 写入缓存
    合并全部格子 → 按 fid 去重 → 按精确范围过滤 → 产出

对上层完全透明:仍是 Building3DSource,fetch/count 签名不变,
清洗/DEM采样/建模/切片四个阶段无需任何改动。

**为何按格而非按"上次下载的范围"**:见 core/building_cache 模块注释——
按范围重叠率阈值复用会让未覆盖区域的建筑静默缺失。
"""
from __future__ import annotations

from typing import Iterator

from ..core.building_cache import (
    BuildingCache,
    Cell,
    cells_for_bbox,
    feature_key,
    feature_to_geojson,
    geojson_to_feature,
)
from ..core.logs import logger
from .buildings import Building3DSource, BuildingFeature


class CachedBuildingSource(Building3DSource):
    """给数据源套一层网格缓存。"""

    def __init__(self, inner: Building3DSource, *, cache_key: str = "",
                 ttl_days: float = 30.0, use_cache: bool = True,
                 use_remote: bool = True):
        self.inner = inner
        self.use_cache = use_cache
        # 缓存目录按"数据内容"分而非按 provider key 分:OSM 数据无论来自
        # Overpass、pbf 解析还是远端数据包,都是同一份东西,应当互相复用。
        key = cache_key or _content_key(inner)
        self.cache = BuildingCache(key, ttl_days=ttl_days)
        # 远端数据包(COS):未配置地址时 enabled 为 False,自动跳过这一级
        self.remote = None
        if use_remote:
            try:
                from ..core.remote_bundles import RemoteBundleStore
                store = RemoteBundleStore(key, cache=self.cache)
                self.remote = store if store.enabled else None
            except Exception as e:
                logger.warning("远端数据包不可用(将回落联网抓取):%s", str(e)[:140])

    # 展示名沿用内层,但标注走缓存
    @property
    def key(self) -> str:      # type: ignore[override]
        return self.inner.key

    @property
    def label(self) -> str:    # type: ignore[override]
        return f"{self.inner.label}(本地缓存)"

    def __getattr__(self, name):
        """未定义的属性委托给内层数据源。

        让装饰器对上层"透明":如 Overture 的 release 属性(runner 会读它来
        提前探测数据版本),不必因为套了缓存就丢掉。
        注意 __getattr__ 只在常规查找失败后调用,不会遮蔽本类已有的属性。
        """
        return getattr(self.inner, name)

    # ---------- 取数 ----------

    def fetch(self, bbox, *, on_progress=None, should_stop=None) -> Iterator[BuildingFeature]:
        cells = cells_for_bbox(bbox)
        if self.use_cache:
            hit, miss = self.cache.split(cells)
            # 本地缺格时先试远端数据包(COS):比 Overpass 快几个数量级,
            # 且免去每台机器重跑 33 分钟的全国 pbf 导入。
            if miss and self.remote is not None and self.remote.enabled:
                if self.remote.covers(bbox):
                    self.remote.fetch_bbox(bbox, should_stop=should_stop)
                    hit, miss = self.cache.split(cells)   # 取回后重新判定
                    logger.info("远端数据包取回后:命中 %d,仍需联网 %d",
                                len(hit), len(miss))
        else:
            # 强制刷新:全部重抓(仍会回写缓存,后续任务受益)
            hit, miss = [], list(cells)
        logger.info("建筑缓存:范围覆盖 %d 格,命中 %d,需抓取 %d(%s)",
                    len(cells), len(hit), len(miss),
                    "强制刷新" if not self.use_cache else f"有效期 {self.cache.ttl_seconds / 86400:.0f} 天")

        # ---- 逐格抓取缺失的 ----
        for i, cell in enumerate(miss, start=1):
            if should_stop and should_stop():
                logger.info("建筑取数被中断(已抓取 %d/%d 格)", i - 1, len(miss))
                return
            logger.info("建筑缓存:抓取第 %d/%d 格 %s", i, len(miss), cell)
            gj_feats: list[dict] = []
            try:
                for feat in self.inner.fetch(cell.bounds, should_stop=should_stop):
                    gj_feats.append(feature_to_geojson(feat))
            except Exception as e:
                # 单格失败不写缓存(避免把空结果当成"已抓过"),但继续其余格子:
                # 已抓到的格子仍能用,下次只补这一格。
                logger.warning("建筑缓存:格 %s 抓取失败,跳过(下次会重试):%s",
                               cell, str(e)[:160])
                if should_stop and should_stop():
                    return
                continue
            if should_stop and should_stop():
                return       # 中断时不写缓存:半个格子的数据不能当完整格
            self.cache.write(cell, gj_feats, source=self.inner.key)
            if on_progress:
                on_progress(i, len(miss))

        # ---- 合并全部格子并去重 ----
        seen: set[str] = set()
        west, south, east, north = bbox
        emitted = 0
        for cell in cells:
            if should_stop and should_stop():
                return
            for gf in self.cache.read(cell):
                k = feature_key(gf)
                if k in seen:
                    continue        # 跨格边界的建筑会在相邻格各存一份
                seen.add(k)
                feat = geojson_to_feature(gf)
                if feat is None:
                    continue
                # 格子比请求范围大,按精确范围过滤(外环包围盒相交即保留,
                # 避免漏掉跨边界的大建筑)
                xs = [p[0] for p in feat.rings[0]]
                ys = [p[1] for p in feat.rings[0]]
                if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                    continue
                emitted += 1
                yield feat
        logger.info("建筑缓存:合并 %d 格产出 %d 栋(去重前 %d)",
                    len(cells), emitted, len(seen))

    def count(self, bbox) -> int:
        """全部格子都已缓存时按缓存计数(零网络);否则委托内层。"""
        cells = cells_for_bbox(bbox)
        if self.use_cache:
            _hit, miss = self.cache.split(cells)
            # 缺格时先试远端:否则为了一个进度分母就去请求 Overpass 很不值
            if miss and self.remote is not None and self.remote.enabled \
                    and self.remote.covers(bbox):
                self.remote.fetch_bbox(bbox)
                _hit, miss = self.cache.split(cells)
            if not miss:
                n = sum((self.cache.meta_of(c) or {}).get("feature_count", 0) for c in cells)
                logger.info("建筑缓存:全部 %d 格命中,按缓存计数 %d(不发请求)", len(cells), n)
                return int(n)
        return self.inner.count(bbox)


def _content_key(inner: Building3DSource) -> str:
    """缓存目录名:按数据内容归类,而非按具体取数途径。

    Overpass 与(将来的)pbf 解析产出的都是 OSM 建筑,应共用同一份缓存;
    Overture 是另一份数据,单独存。
    """
    k = str(getattr(inner, "key", "") or "")
    if "osm" in k or "overpass" in k:
        return "osm"
    if "overture" in k:
        return "overture"
    return k or "unknown"


def wrap_with_cache(inner: Building3DSource, *, use_cache: bool = True,
                    ttl_days: float = 30.0) -> Building3DSource:
    """给数据源套缓存。本地上传的矢量面无需缓存(数据已在本地)。"""
    if getattr(inner, "key", "") == "local_vector":
        return inner
    return CachedBuildingSource(inner, use_cache=use_cache, ttl_days=ttl_days)
