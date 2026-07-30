"""天地图 EPSG:4326(经纬度)瓦片方案的行列号换算。

天地图 img_c / vec_c 使用 TileMatrixSet="c":
  - 瓦片 256x256,原点在左上角 (lon=-180, lat=90)
  - 第 z 级:列数=2^z,行数=2^(z-1)
  - 每张瓦片的经纬跨度 d = 360 / 2^z(经、纬方向相等)

约定:col 沿经度自西向东递增,row 沿纬度自北向南递增。
"""
from __future__ import annotations

from dataclasses import dataclass

TILE_SIZE = 256
ORIGIN_LON = -180.0
ORIGIN_LAT = 90.0


def tile_span(z: int) -> float:
    """第 z 级单张瓦片的经纬跨度(度)。"""
    return 360.0 / (2 ** z)


def matrix_size(z: int) -> tuple[int, int]:
    """第 z 级的 (列数, 行数)。"""
    return 2 ** z, 2 ** (z - 1) if z >= 1 else 1


def lonlat_to_tile(lon: float, lat: float, z: int) -> tuple[int, int]:
    """经纬度 → (col, row)。"""
    d = tile_span(z)
    col = int((lon - ORIGIN_LON) / d)
    row = int((ORIGIN_LAT - lat) / d)
    return col, row


def tile_bounds(col: int, row: int, z: int) -> tuple[float, float, float, float]:
    """单张瓦片的地理四至 (west, south, east, north),单位度。"""
    d = tile_span(z)
    west = ORIGIN_LON + col * d
    east = west + d
    north = ORIGIN_LAT - row * d
    south = north - d
    return west, south, east, north


@dataclass
class TileRange:
    """某一级别下,一个矩形范围覆盖到的瓦片行列区间(闭区间)。"""
    z: int
    col_min: int
    col_max: int
    row_min: int
    row_max: int

    @property
    def cols(self) -> int:
        return self.col_max - self.col_min + 1

    @property
    def rows(self) -> int:
        return self.row_max - self.row_min + 1

    @property
    def count(self) -> int:
        return self.cols * self.rows

    def iter_tiles(self):
        """遍历区间内所有 (col, row)。"""
        for row in range(self.row_min, self.row_max + 1):
            for col in range(self.col_min, self.col_max + 1):
                yield col, row

    def mosaic_bounds(self) -> tuple[float, float, float, float]:
        """整个瓦片区间拼接后的地理四至 (west, south, east, north)。"""
        w, _, _, n = tile_bounds(self.col_min, self.row_min, self.z)
        _, s, e, _ = tile_bounds(self.col_max, self.row_max, self.z)
        return w, s, e, n


def range_for_bbox(
    west: float, south: float, east: float, north: float, z: int
) -> TileRange:
    """给定经纬度矩形范围与级别,计算覆盖的瓦片行列区间。

    对边界做钳制,避免越出该级别的矩阵范围。
    """
    max_col, max_row = matrix_size(z)
    max_col -= 1
    max_row -= 1

    col_min, row_min = lonlat_to_tile(west, north, z)   # 左上角
    col_max, row_max = lonlat_to_tile(east, south, z)   # 右下角

    col_min = max(0, min(col_min, max_col))
    col_max = max(0, min(col_max, max_col))
    row_min = max(0, min(row_min, max_row))
    row_max = max(0, min(row_max, max_row))

    if col_min > col_max:
        col_min, col_max = col_max, col_min
    if row_min > row_max:
        row_min, row_max = row_max, row_min

    return TileRange(z, col_min, col_max, row_min, row_max)


def estimate_total_tiles(
    bbox: tuple[float, float, float, float], z_min: int, z_max: int
) -> int:
    """估算级别区间内需要下载的瓦片总数,用于任务提交前预估。"""
    w, s, e, n = bbox
    return sum(range_for_bbox(w, s, e, n, z).count for z in range(z_min, z_max + 1))


def estimate_levels(
    bbox: tuple[float, float, float, float], levels: list[int]
) -> int:
    """按选中的级别列表估算瓦片总数。"""
    w, s, e, n = bbox
    return sum(range_for_bbox(w, s, e, n, z).count for z in levels)


# 各数据源单瓦片平均字节数(经验值,用于下载前估算大小;真实值随影像内容波动)
AVG_TILE_BYTES = {
    "tianditu_img": 15 * 1024,   # 影像 JPEG
    "tianditu_vec": 10 * 1024,   # 矢量底图 PNG
    "tianditu_ter": 15 * 1024,   # 地形晕渲 JPEG
}
DEFAULT_TILE_BYTES = 15 * 1024


def estimate_levels_detail(
    bbox: tuple[float, float, float, float],
    levels: list[int],
    provider: str = "tianditu_img",
) -> dict:
    """按选中级别逐层估算瓦片数与字节数,并给出合计。

    返回 {levels:[{z,tiles,bytes}], total_tiles, total_bytes}。
    """
    w, s, e, n = bbox
    avg = AVG_TILE_BYTES.get(provider, DEFAULT_TILE_BYTES)
    per = []
    total_tiles = 0
    for z in sorted(set(levels)):
        tiles = range_for_bbox(w, s, e, n, z).count
        per.append({"z": z, "tiles": tiles, "bytes": tiles * avg})
        total_tiles += tiles
    return {
        "levels": per,
        "total_tiles": total_tiles,
        "total_bytes": total_tiles * avg,
    }
