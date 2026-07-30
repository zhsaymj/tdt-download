"""三维建筑轮廓数据源抽象。

与 TileProvider(栅格瓦片,按 z/col/row 拼 URL)是**两套不同的抽象**:
建筑数据是矢量要素集,按 bbox 一次性查询,没有瓦片行列号概念,
故独立成 Building3DSource 接口,不复用 TileProvider。

第一个实现是 Overture Maps(providers/overture.py)。后续接实景 3D Tiles
时实现同一接口即可,上层管线(core/buildings.py → b3dm → tileset)不变。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator


@dataclass
class BuildingFeature:
    """单栋建筑要素(已归一化,坐标为 WGS84 经纬度)。

    rings:外环 + 内环(洞)列表,每环为 [(lon, lat), ...],不闭合(首尾不重复)。
           rings[0] 为外环,其余为洞。
    height:建筑总高(米,相对地面)。None 表示原始数据缺失,由清洗阶段补全。
    num_floors:层数,height 缺失时用于推算。
    """
    fid: str
    rings: list[list[tuple[float, float]]]
    height: float | None = None
    num_floors: int | None = None
    name: str = ""
    #: 高度来源,清洗阶段回填:height/num_floors/area_estimate/default
    height_source: str = ""
    #: 底面海拔(米),由 DEM 采样阶段回填
    base_height: float = 0.0
    props: dict = field(default_factory=dict)


class Building3DSource(ABC):
    """三维建筑数据源接口。"""

    #: 数据源标识,如 "overture_buildings"
    key: str = "buildings"
    #: 展示名
    label: str = "建筑轮廓"

    @abstractmethod
    def fetch(
        self,
        bbox: tuple[float, float, float, float],
        *,
        on_progress=None,
        should_stop=None,
    ) -> Iterator[BuildingFeature]:
        """按 bbox 拉取建筑要素。

        bbox: (west, south, east, north) WGS84。
        on_progress(fetched: int, total: int | None): 可选进度回调,
            total 未知时传 None(远端不预先给总数)。
        should_stop(): 返回 True 时应尽快中断迭代。
        """
        raise NotImplementedError

    def count(self, bbox: tuple[float, float, float, float]) -> int:
        """预估该范围内建筑数量(供提交前估算,不必精确)。默认 0 表示未知。"""
        return 0


# ---------- 数据源登记 ----------
# 放在抽象模块而非某个具体实现里:runner/models/api 都要据此判断"是否走三维建筑
# 管线",这个判断与用哪个数据源无关。

#: provider key -> 展示名
BUILDING_LAYERS = {
    "osm_buildings": "OSM 建筑轮廓(Overpass,境内可直连)",
    "overture_buildings": "Overture 建筑轮廓(全球,需境外网络)",
    "local_vector": "本地矢量面(上传房屋轮廓)",
}

#: 默认数据源。选 OSM 而非 Overture:Overture 的 512 个 parquet 分片需跨境
#: 读遍 footer 才能做 bbox 裁剪,境内实测单次查询十几小时,不可用。
DEFAULT_BUILDING_PROVIDER = "osm_buildings"

BUILDING_PROVIDERS = set(BUILDING_LAYERS)


def is_building_provider(key: str) -> bool:
    return key in BUILDING_PROVIDERS


def build_building_source(key: str = DEFAULT_BUILDING_PROVIDER,
                          release: str | None = None,
                          proxy: str | None = None,
                          **kw) -> "Building3DSource":
    """按 key 构造建筑数据源。

    release 仅 Overture 用到;proxy 两者都支持。延迟导入避免循环依赖
    (各实现模块都要 import 本模块的 BuildingFeature)。
    """
    if key == "osm_buildings":
        from .overpass import build_overpass_source
        return build_overpass_source(proxy=proxy, **kw)
    if key == "overture_buildings":
        from .overture import OvertureBuildingSource
        return OvertureBuildingSource(release=release, proxy=proxy)
    if key == "local_vector":
        from .local_vector import build_local_vector_source
        return build_local_vector_source(**kw)
    raise ValueError(f"暂不支持的建筑数据源:{key}")
