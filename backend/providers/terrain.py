"""真实高程 DEM 数据源:Esri World Elevation / Terrain3D。

这是**真实海拔高程**数据(非晕渲图片):
  - 服务:elevation3d.arcgis.com/.../WorldElevation3D/Terrain3D/ImageServer
  - 坐标系 EPSG:3857 Web 墨卡托,瓦片金字塔与我们的 XYZ 网格同构,缩放 0~16
  - 瓦片体是 **LERC(v1)压缩的 F32 高程**(单位:米),需用 lerc 库解码
  - URL:{root}/tile/{z}/{row}/{col}(row 对应纬度/y,col 对应经度/x)
  - Esri 免费公开服务,直连、无 token;需带模拟客户端请求头

因坐标方案与天地图不同,DEM 走独立的下载→LERC 解码→拼接管线
(core/dem_tiling.py + core/dem.py),不复用天地图的 4326 换算与图像拼接。
但网格与 AWS Terrarium/OSM 同为墨卡托 XYZ,复用 dem_tiling 的换算。
"""
from __future__ import annotations

from .base import TileProvider

# Esri Terrain3D 服务根地址
ESRI_TERRAIN_ROOT = (
    "https://elevation3d.arcgis.com/arcgis/rest/services"
    "/WorldElevation3D/Terrain3D/ImageServer"
)

# 模拟 ArcGIS 客户端请求头(Esri 公共服务保险起见带上)
ESRI_HEADERS = {
    "User-Agent": "ArcGISRuntime-NET/100.9 (Windows) ArcGISEarth/1.11",
    "Referer": "http://esri.arcgisearth.app/",
    "Accept-Encoding": "gzip,deflate",
}

# DEM 数据源标识集合(runner 据此判断走 DEM 管线)
DEM_PROVIDERS = {"esri_terrain"}

# 数据源元数据:key -> (瓦片后缀, 中文名, 最大级别)
# 瓦片体为 LERC,缓存扩展名用 lerc 以示区分(非图片)。
DEM_LAYERS = {
    "esri_terrain": ("lerc", "全国地形 DEM(Esri Terrain3D)", 16),
}


class TerrainProvider(TileProvider):
    """Esri Terrain3D DEM 数据源。EPSG:3857 墨卡托 XYZ,LERC 编码,无需密钥。"""

    def __init__(self, key: str = "esri_terrain"):
        if key not in DEM_LAYERS:
            raise ValueError(f"暂不支持的 DEM 数据源:{key}")
        ext, _cn, zmax = DEM_LAYERS[key]
        self.key = key
        self.ext = ext
        self.bands = 1          # 成果为单波段高程;瓦片为 LERC 编码,解码后单波段
        self._zmax = zmax

    @property
    def headers(self) -> dict:
        return ESRI_HEADERS

    def tile_url(self, col: int, row: int, z: int) -> str:
        # 下载器约定 tile_url(col=x, row=y, z);Esri URL 为 /tile/{z}/{row}/{col}
        return f"{ESRI_TERRAIN_ROOT}/tile/{z}/{row}/{col}"

    def min_zoom(self) -> int:
        return 0

    def max_zoom(self) -> int:
        return self._zmax


def is_dem_provider(key: str) -> bool:
    return key in DEM_PROVIDERS


def build_terrain_provider(key: str = "esri_terrain") -> TerrainProvider:
    return TerrainProvider(key)


def probe_max_level(bbox: tuple[float, float, float, float],
                    key: str = "esri_terrain") -> int:
    """探测某范围在 Esri Terrain3D 上的最高可用级别(有真实数据的最大 LOD)。

    Esri 超出某区域最高 LOD 时返回空瓦片(约 67 字节)而非 404,且最高 LOD
    随地理位置变化(城市/发达地区更高)。这里从服务最高级往下,取范围中心
    瓦片逐级探测,返回第一个非空级别;全空则回退到 min_zoom。
    """
    import urllib.error
    import urllib.request

    from ..core.dem import is_empty_lerc
    from ..core.dem_tiling import mercator_range_for_bbox

    provider = build_terrain_provider(key)
    zmax, zmin = provider.max_zoom(), provider.min_zoom()

    def center_tile_empty(z: int) -> bool:
        tr = mercator_range_for_bbox(*bbox, z)
        cx = (tr.col_min + tr.col_max) // 2
        cy = (tr.row_min + tr.row_max) // 2
        url = provider.tile_url(cx, cy, z)
        try:
            req = urllib.request.Request(url, headers=provider.headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                if resp.status != 200:
                    return True
                return is_empty_lerc(resp.read())
        except urllib.error.HTTPError:
            return True
        except Exception:
            # 网络异常无法判定,保守认为该级不可用
            return True

    for z in range(zmax, zmin - 1, -1):
        if not center_tile_empty(z):
            return z
    return zmin
