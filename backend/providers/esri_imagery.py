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
