"""Google 影像数据源(非官方瓦片端点,EPSG:3857 墨卡托 XYZ)。

实测结论(2026-09-25,经本机代理):
  - **必须走代理**:直连全部超时(DNS 能解析,TCP 连不上)
  - 端点**忽略 key 参数**:带/不带/空 key 返回字节完全相同的瓦片,
    故配置里刻意不设 key 字段,避免"填了 key 才有权限"的误解
  - **无 200 占位图**:无影像处返回 404 + 标准 Google 错误页,
    故 is_empty_tile 恒 False,由 missing_statuses 声明 404
  - **无地区性级别降级**:陆地处处可到 z21(含拉萨、乌鲁木齐等西部城市),
    z22 仅部分地区有 —— 故不需要像 Esri 那样按区域探测最高级别
  - lyrs=m(路线图)返回 **count=1 的调色板 PNG**,不是 3 波段

已知数据质量问题:部分区域(如北京北四环 z16)有拼接接缝与纹理重复,
是数据源自身问题而非本工具缺陷(坐标已实证正确,与 Esri 偏移 ≤4m)。
"""
from __future__ import annotations

import itertools

from .base import TileProvider, normalize_proxy

#: key -> (lyrs 代码, 瓦片后缀, 波段数, 中文名)
#: google_road 的波段数是 1:实测 lyrs=m 返回单波段调色板 PNG,
#: 按 3 波段处理会在拼接时抛 DatasetIOShapeError。
#: mosaic._read_tile 已有调色板展开逻辑,登记为 1 即可复用。
GOOGLE_LAYERS: dict[str, tuple[str, str, int, str]] = {
    "google_img": ("s", "jpg", 3, "Google 卫星影像"),
    "google_hybrid": ("y", "jpg", 3, "Google 影像(含路网)"),
    "google_road": ("m", "png", 1, "Google 路线图"),
    "google_terrain": ("p", "jpg", 3, "Google 地形"),
}


class GoogleProvider(TileProvider):
    """Google 瓦片数据源。EPSG:3857 墨卡托 XYZ,无需密钥,需代理。"""

    def __init__(self, key: str, cfg):
        if key not in GOOGLE_LAYERS:
            raise ValueError(f"暂不支持的 Google 图层:{key}")
        if not (cfg.url_template or "").strip():
            raise ValueError("google.url_template 为空,请检查 config.yaml")
        lyrs, ext, bands, _cn = GOOGLE_LAYERS[key]
        self.key = key
        self.lyrs = lyrs
        self.ext = ext
        self.bands = bands
        self.cfg = cfg
        self._sub = itertools.cycle(cfg.subdomain_list())

    @property
    def proxy(self) -> str | None:
        # 必须归一化:配置里常写 host:port,直接传给 aiohttp 会抛 InvalidURL
        return normalize_proxy(self.cfg.proxy)

    def tile_url(self, col: int, row: int, z: int) -> str:
        sub = next(self._sub)
        return self.cfg.url_template.format(s=sub, lyrs=self.lyrs,
                                            x=col, y=row, z=z)

    def min_zoom(self) -> int:
        return 1

    def max_zoom(self) -> int:
        return int(self.cfg.max_zoom)

    def is_empty_tile(self, data: bytes) -> bool:
        """恒 False —— 这是实测结论,不是"未实现"。

        Google 在无影像处返回 HTTP 404 + 错误页,从不返回 200 占位图
        (在 4 个位置 × 7 个级别上验证过)。无数据的识别走 missing_statuses。
        """
        return False

    def missing_statuses(self) -> frozenset[int]:
        """404 = 该瓦片无影像(海洋/极地/无覆盖)。

        下载器据此不重试、不计失败。若按普通失败处理,一个纯海域选区
        会让每张瓦片白跑 3 次重试,且失败率触发"端点可能已变更"的误报。
        """
        return frozenset({404})


def is_google_provider(key: str) -> bool:
    return key in GOOGLE_LAYERS


def build_google_provider(key: str, cfg) -> GoogleProvider:
    return GoogleProvider(key, cfg)
