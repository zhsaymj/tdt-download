"""天地图瓦片数据源(EPSG:4326 / TileMatrixSet=c)。

支持图层:
  - img(影像)、vec(矢量底图)、ter(地形晕渲图,非高程数值)

WMTS KVP 请求示例:
  https://t0.tianditu.gov.cn/img_c/wmts?SERVICE=WMTS&REQUEST=GetTile
    &VERSION=1.0.0&LAYER=img&STYLE=default&TILEMATRIXSET=c
    &FORMAT=tiles&TILEMATRIX={z}&TILEROW={row}&TILECOL={col}&tk={token}

t0~t7 为多个子域名,轮询使用可提高并发、降低单域限制。
"""
from __future__ import annotations

import itertools
from typing import Callable, Union

from .base import TileProvider

# token 可以是固定字符串(如底图),也可以是每次调用动态返回密钥的可调用对象
# (tk 使用池:每生成一次瓦片 URL 即取当前密钥并计数)。
TokenSource = Union[str, Callable[[], str]]

SUBDOMAINS = ["t0", "t1", "t2", "t3", "t4", "t5", "t6", "t7"]

# 图层元数据:key -> (服务前缀 layerType, LAYER 名, 瓦片后缀, 波段数, 中文名)
# 注记(cia/cva/cta)是带透明通道的 PNG 覆盖层,与底图同网格,勾选后叠加。
LAYERS = {
    "tianditu_img": ("img_c", "img", "jpg", 3, "天地图影像"),
    "tianditu_vec": ("vec_c", "vec", "png", 3, "天地图矢量底图"),
    "tianditu_ter": ("ter_c", "ter", "jpg", 3, "天地图地形晕渲"),
    "tianditu_cia": ("cia_c", "cia", "png", 4, "天地图影像注记"),
    "tianditu_cva": ("cva_c", "cva", "png", 4, "天地图矢量注记"),
    "tianditu_cta": ("cta_c", "cta", "png", 4, "天地图地形注记"),
}

# 底图数据源 -> 对应注记图层 key(勾选「叠加路网注记」时同步下载)
ANNOTATION_OF = {
    "tianditu_img": "tianditu_cia",
    "tianditu_vec": "tianditu_cva",
    "tianditu_ter": "tianditu_cta",
}


class TiandituProvider(TileProvider):
    """天地图 EPSG:4326 瓦片数据源(可切换 img/vec/ter 图层)。"""

    def __init__(self, key: str, token: TokenSource):
        if key not in LAYERS:
            raise ValueError(f"暂不支持的天地图图层:{key}")
        # token 可为字符串或可调用(动态取密钥);字符串为空时报错,可调用留待运行时解析
        if not callable(token) and not token:
            raise ValueError("天地图密钥(token)为空,请在 config.yaml 或密钥管理中填写。")
        layer_type, layer_name, ext, bands, _cn = LAYERS[key]
        self.key = key
        self.layer_type = layer_type
        self.layer_name = layer_name
        self.ext = ext
        self.bands = bands
        self._token = token
        self._sub = itertools.cycle(SUBDOMAINS)

    def _resolve_token(self) -> str:
        """取当前密钥:可调用则每次动态获取(tk 池计数),否则用固定串。"""
        return self._token() if callable(self._token) else self._token

    def tile_url(self, col: int, row: int, z: int) -> str:
        sub = next(self._sub)
        return (
            f"https://{sub}.tianditu.gov.cn/{self.layer_type}/wmts?"
            f"SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER={self.layer_name}"
            f"&STYLE=default&TILEMATRIXSET=c&FORMAT=tiles"
            f"&TILEMATRIX={z}&TILEROW={row}&TILECOL={col}&tk={self._resolve_token()}"
        )

    def min_zoom(self) -> int:
        return 1

    def max_zoom(self) -> int:
        return 18


def build_provider(key: str, token: TokenSource) -> TileProvider:
    """按数据源标识构造 provider。后续 DEM 等在此登记。

    token 可为固定字符串或可调用(tk 使用池:每次取 URL 时动态取密钥并计数)。
    """
    if key in ("img",):
        key = "tianditu_img"
    if key in LAYERS:
        return TiandituProvider(key, token)
    raise ValueError(f"暂不支持的数据源:{key}")


def build_annotation_provider(base_key: str, token: TokenSource) -> TiandituProvider | None:
    """按底图数据源构造对应的注记 provider;无对应注记时返回 None。"""
    if base_key in ("img",):
        base_key = "tianditu_img"
    anno_key = ANNOTATION_OF.get(base_key)
    return TiandituProvider(anno_key, token) if anno_key else None
