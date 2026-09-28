"""Esri World Imagery 数据源(EPSG:3857 墨卡托 XYZ)。

与项目现用的 Esri Terrain3D(providers/terrain.py,DEM)是**不同服务**:
Terrain3D 直连可用,World Imagery 实测直连不通,必须走代理。

两个必须编码进实现的实测结论(2026-09-25):

1. **占位图与级别无关**。Esri 在"该处无影像"时返回 HTTP 200 + 2521 字节的
   固定占位图("Map data not yet available")。它出现在两种情况:
     - 超出该地区最高可用级别(北京 z20、阿里 z18)
     - 该处本来就没有影像(海洋、极地、格陵兰冰盖)
   后者在**任何级别**都会命中 —— 故 is_empty_tile 是正常路径的必需组件,
   不是防御性实现。

2. **最高可用级别随地区变化**。城市可到 z19,西藏/青海/新疆无人区只到 z17
   (z18 即整片占位图)。故需要 probe_max_level,与 terrain.py 同款模式。

URL 顺序是 /tile/{z}/{y}/{x} —— 注意 y 在 x 之前(ArcGIS 约定)。
"""
from __future__ import annotations

import hashlib
import io

from .base import TileProvider, normalize_proxy

#: 实测 "Map data not yet available" 占位图的 sha256。
#: 该图跨大洲(北京/上海/喀什/阿里/南海/南极/格陵兰)、跨级别字节**完全一致**
#: (2026-09-25 实测),故精确匹配即可零误判。
#: 对应夹具:tests/fixtures/esri_wi_placeholder_2521B.jpg
PLACEHOLDER_SHA256 = (
    "9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858")

#: 兜底启发式的阈值。仅在 sha 指纹失效(Esri 改版)时才用到。
#: 阈值由实测正负样本校准,两侧都留足裕量:
#:   占位图  mean=204.73  std=5.37   size=2521
#:   真实瓦片 mean 最高 154.68、std 最低 14.96、size 范围 678~25524
#: 三个条件必须**同时**满足 —— 只看体量或色值数会把 678 字节的真实深海瓦片
#: (唯一色值仅 11)误判成占位图。
_FALLBACK_SIZE_MIN = 2000
_FALLBACK_SIZE_MAX = 3000
_FALLBACK_MEAN_MIN = 190.0
_FALLBACK_STD_MAX = 10.0

ESRI_IMAGERY_KEY = "esri_imagery"


class EsriImageryProvider(TileProvider):
    """Esri World Imagery。EPSG:3857 墨卡托 XYZ,无需密钥,需代理。"""

    key = ESRI_IMAGERY_KEY
    ext = "jpg"
    bands = 3

    def __init__(self, cfg):
        if not (cfg.url_template or "").strip():
            raise ValueError("esri_imagery.url_template 为空,请检查 config.yaml")
        self.cfg = cfg

    @property
    def proxy(self) -> str | None:
        return normalize_proxy(self.cfg.proxy)

    def tile_url(self, col: int, row: int, z: int) -> str:
        # ArcGIS 顺序是 {z}/{y}/{x}:y(row)在 x(col)之前。
        # 写反会取到地理位置错误的瓦片 —— 图像看着正常,坐标全错。
        return self.cfg.url_template.format(z=z, y=row, x=col)

    def min_zoom(self) -> int:
        return 1

    def max_zoom(self) -> int:
        return int(self.cfg.max_zoom)

    def is_empty_tile(self, data: bytes) -> bool:
        """该响应是否为"此处无影像"的占位图。

        两级判定:
          1. **精确指纹(主)**:sha256 命中即判定。零误判,但 Esri 改版会失效。
          2. **保守启发式(兜底)**:体量贴近 2521、极亮、几乎无纹理。

        启发式刻意**宁可漏判不可误判**:
          - 漏判(占位图当真实)的后果是成果出现灰色方块 —— 肉眼可见、可重跑
          - 误判(真实瓦片当无数据)的后果是**静默的数据空洞**,且瓦片不入缓存,
            每次重跑都重新下载
        故阈值偏向"判为真实"。实测的关键负样本是一张 678 字节的深海瓦片
        (mean 19.99、唯一色值 11),它比占位图更小更单调,必须放行。
        """
        if not data:
            return False
        if hashlib.sha256(data).hexdigest() == PLACEHOLDER_SHA256:
            return True
        # 兜底:先用体量快速排除,避免对每张正常瓦片都解码
        if not (_FALLBACK_SIZE_MIN <= len(data) <= _FALLBACK_SIZE_MAX):
            return False
        try:
            mean, std = _mean_std(data)
        except Exception:
            # 解码失败:不是占位图,交给上层按失败处理(不能当"无数据"吞掉)
            return False
        return mean > _FALLBACK_MEAN_MIN and std < _FALLBACK_STD_MAX


def _mean_std(data: bytes) -> tuple[float, float]:
    """解码瓦片并返回全波段的 (均值, 标准差)。

    只在兜底路径调用(体量已落在 2000~3000 的窄区间),故解码开销可忽略。
    """
    import numpy as np
    import rasterio

    with rasterio.io.MemoryFile(io.BytesIO(data)) as mem:
        with mem.open() as ds:
            arr = ds.read()
    return float(np.asarray(arr).mean()), float(np.asarray(arr).std())


def is_esri_imagery_provider(key: str) -> bool:
    return key == ESRI_IMAGERY_KEY


def build_esri_imagery_provider(cfg) -> EsriImageryProvider:
    return EsriImageryProvider(cfg)


# ---------- 区域最高级别探测 ----------
#
# 为什么必须有:实测 Esri 的最高可用级别**随地理位置变化** ——
#   上海/北京/拉萨/乌鲁木齐/纽约/伦敦/东京/悉尼:z19 有数据
#   喀什/漠河                                  :z18(z19 全是占位图)
#   格尔木/可可西里/塔克拉玛干/阿里             :z17(z18 已是整片占位图)
# 用户按全局 max_zoom 选级别,在西部会下到一整片灰色占位图。
#
# ⚠️ 上述级别用 **5x5 网格采样**判定,不能用单张瓦片 —— 单点会被
# "该处恰好无影像"或"该次请求恰好失败"污染。实测同一张真实瓦片连取 4 次
# 有 1 次失败,单次尝试足以把 z19 误判成 z18(拉萨、乌鲁木齐初版就是这么错的)。
#
# 这与项目现有 DEM 的行为完全同构(providers/terrain.py::probe_max_level
# 的注释原文:"最高 LOD 随地理位置变化,新疆等西部区域实测只到 14 级"),
# 故沿用同一模式而非重新设计。
#
# ⚠️ 与 terrain 版的关键差别:**必须走代理**。
# Terrain3D 直连可用,故它用 urllib 直连是合理的;World Imagery 直连全部超时,
# 照抄会让探测永远失败并静默降级到最低级别。

#: 探测结果缓存:量化后的 bbox key -> (最高级别, 写入时间戳)
_probe_cache: dict[tuple, tuple[int, float]] = {}

#: bbox 量化粒度(度)。用户拖拽选区时相邻请求会落到同一格,复用结果。
_PROBE_GRID_DEG = 0.1


def _probe_cache_key(bbox, cap: int) -> tuple:
    w, s, e, n = bbox
    q = _PROBE_GRID_DEG
    return (round(w / q), round(s / q), round(e / q), round(n / q), cap)


def _fetch_tile_bytes(provider: EsriImageryProvider, col: int, row: int,
                      z: int, timeout: int = 10) -> bytes | None:
    """同步取一张瓦片;失败返回 None。

    用 urllib 而非 aiohttp:本函数由 api 层放进 asyncio.to_thread 调用
    (与现有 _probe_dem_max_level 一致),在线程里再起事件循环会把简单的事复杂化。

    必须带代理 —— 见本节顶部的警告。
    """
    import urllib.error
    import urllib.request

    url = provider.tile_url(col, row, z)
    proxy = provider.proxy
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy,
                                                   "https": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=provider.headers)
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read()
    except urllib.error.HTTPError:
        # 明确的 HTTP 错误:当作"该级别没有"(与 terrain 版一致)
        return b""
    except Exception:
        # 网络/代理问题:判不了
        return None


#: 单级探测的重试次数。实测代理下 Esri 偶发取不到瓦片(同一张瓦片连取 4 次,
#: 第 2 次返回失败、其余 3 次正常),不重试就会把网络抖动当成"该级无数据"。
_PROBE_ATTEMPTS = 3


def _fetch_tile_with_retry(provider: EsriImageryProvider, col: int, row: int,
                           z: int, timeout: int = 10) -> bytes | None:
    """取一张瓦片,失败重试;全失败返回 None。

    重试是必需的,不是保险:实测同一张真实瓦片连取 4 次会有 1 次失败。
    若单次失败就下探到低一级,用户会**静默损失一个缩放级别**
    (实测上海 z19 有数据,却因一次抖动被探测成 z18)。
    """
    for attempt in range(_PROBE_ATTEMPTS):
        data = _fetch_tile_bytes(provider, col, row, z, timeout=timeout)
        if data is not None:
            return data
    return None


def probe_max_level(bbox: tuple[float, float, float, float],
                    cfg=None, timeout: int = 10) -> int | None:
    """探测 Esri World Imagery 在某范围的最高可用级别。

    从服务级天花板(cfg.max_zoom)往下,取范围中心瓦片逐级探测,
    返回第一个有真实影像的级别。

    返回 None 表示**无法判定**(网络不通/代理未配)。调用方据此放行用户选的
    级别,而不是误判成"该范围没有影像"把级别降到最低 —— 与 terrain 版的
    返回值语义一致(providers/terrain.py:129 的注释)。

    逐级都是占位图(如公海)则返回 min_zoom 兜底:该范围确实无影像。

    **单点采样的固有局限**(记录备查,不额外处理):探的只是选区中心那一张
    瓦片。若中心恰是占位图而周边有数据,会少报一级;反之会多报。实测
    0.01° 选区的 4x4 抽查里同级别取值一致,故按单点采样是实现上的合理取舍;
    真正需要精确时应当改成多瓦片表决。
    """
    import time as _time

    from ..config import settings
    from ..core.logs import logger
    from ..core.mercator_tiling import mercator_range_for_bbox

    cfg = cfg if cfg is not None else settings.esri_imagery
    provider = EsriImageryProvider(cfg)
    zmax, zmin = provider.max_zoom(), provider.min_zoom()

    cache_hours = float(getattr(cfg, "probe_cache_hours", 0) or 0)
    ckey = _probe_cache_key(bbox, zmax)
    if cache_hours > 0:
        hit = _probe_cache.get(ckey)
        if hit is not None:
            level, ts = hit
            if _time.time() - ts < cache_hours * 3600:
                return level

    determined = False
    found: int | None = None
    for z in range(zmax, zmin - 1, -1):
        tr = mercator_range_for_bbox(*bbox, z)
        cx = (tr.col_min + tr.col_max) // 2
        cy = (tr.row_min + tr.row_max) // 2
        data = _fetch_tile_with_retry(provider, cx, cy, z, timeout=timeout)
        if data is None:
            # 重试后仍取不到:这一级判不了,下探一级。
            # 记一笔 —— 这种情况下报出的级别偏低,用户会少拿一级细节,
            # 不记日志就完全不可见。
            logger.warning("Esri 级别探测:z%s 重试 %s 次仍取不到瓦片,"
                           "该级判不了,下探(%s 张的选区中心瓦片 %s,%s)",
                           z, _PROBE_ATTEMPTS, bbox, cx, cy)
            continue
        determined = True
        if data and not provider.is_empty_tile(data):
            found = z
            break

    if found is None and determined:
        found = zmin          # 逐级都无影像(如公海),取最低级别兜底
    if found is None:
        return None           # 全程网络失败:判不了,不缓存

    if cache_hours > 0:
        _probe_cache[ckey] = (found, _time.time())
    return found
