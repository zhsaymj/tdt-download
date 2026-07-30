"""数据源抽象基类。

影像(天地图)是第一个实现;DEM 等后续数据源实现同一接口后,
下载器与拼接管线即可复用,无需改动上层逻辑。
"""
from __future__ import annotations

from abc import ABC, abstractmethod


class TileProvider(ABC):
    """瓦片数据源接口。"""

    #: 数据源标识,如 "tianditu_img"
    key: str = "base"
    #: 瓦片文件后缀,如 "png" / "jpg" / "tif"
    ext: str = "png"
    #: 波段数(RGB=3,RGBA=4,DEM 单波段=1),供拼接模块使用
    bands: int = 3

    @property
    def headers(self) -> dict:
        """下载该数据源瓦片时附带的 HTTP 请求头(默认无;子类可覆盖)。"""
        return {}

    @abstractmethod
    def tile_url(self, col: int, row: int, z: int) -> str:
        """返回单张瓦片的下载 URL。"""
        raise NotImplementedError

    def min_zoom(self) -> int:
        return 1

    def max_zoom(self) -> int:
        return 18
