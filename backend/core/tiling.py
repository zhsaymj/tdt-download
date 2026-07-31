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


#: 建议级别时的瓦片数软上限。约 5000 张 ≈ 天地图影像 250 MB、十几分钟下载,
#: 是"一次能接受的任务量"的量级。超过它就该缩小范围或降低最高级别,而不是硬下。
SUGGEST_TILE_BUDGET = 5000


def suggest_levels(
    bbox: tuple[float, float, float, float],
    z_max_cap: int = 18,
    z_min_cap: int = 1,
    min_useful_ratio: float = 0.25,
    tile_budget: int = SUGGEST_TILE_BUDGET,
    depth: int = 4,
) -> dict:
    """按选区大小建议该下载哪些级别。

    两个判据合起来用,缺一个都会给出坏建议:

    1) **有效数据占比** = 选区面积 / 该级瓦片区间实际覆盖面积。瓦片是固定网格,
       级别越低单张跨度越大,一张能盖住远超选区的范围——实测 0.07° 的选区在天地图
       第 7 级只有 0.1% 有效占比(那张瓦片覆盖整个市域),下了也只是得到一张几乎
       全是选区外内容的图。低于阈值的级别标 useful=False。

    2) **下载量预算**。只看占比会永远推最高几级:省域选区的 15-18 级有效占比都
       超过 96%,但合计 1762 万张瓦片——这是最坏的建议。故从满足占比的级别里由高
       到低累加瓦片数,超预算就停,保证 recommended 的总量落在可接受范围内。

    极端情况:选区极小时可能没有任何级别达占比阈值(0.007° 的小区到 18 级才 72%),
    此时退回取最高几级——它们仍是最贴合的,且瓦片数很少。

    返回 {levels:[{z,tiles,ratio,useful}], recommended, min_useful, max_useful,
          recommended_tiles, budget_limited}。
    """
    w, s, e, n = bbox
    sel_area = max((e - w) * (n - s), 1e-12)
    rows = []
    for z in range(z_min_cap, z_max_cap + 1):
        tr = range_for_bbox(w, s, e, n, z)
        bw, bs, be, bn = tr.mosaic_bounds()
        cov = max((be - bw) * (bn - bs), 1e-12)
        ratio = min(sel_area / cov, 1.0)
        rows.append({"z": z, "tiles": tr.count, "ratio": round(ratio, 4),
                     "useful": ratio >= min_useful_ratio})

    tiles_of = {r["z"]: r["tiles"] for r in rows}
    useful = [r["z"] for r in rows if r["useful"]]
    pool = useful or [r["z"] for r in rows]

    # 先定"最高可行级别":单是这一级就超预算的话,再往上都没意义(瓦片数按 4 倍
    # 递增)。大范围选区靠这一步把顶降下来——省域选区的 18 级有 1300 万张,
    # 保底也不该建议它,否则建议本身成了最坏方案。
    top = pool[0]
    for z in pool:
        if tiles_of[z] <= tile_budget:
            top = z
        else:
            break
    budget_limited = top < pool[-1]

    # 再从 top 往低走累加,凑够 depth 层或用完预算
    picked: list[int] = []
    total = 0
    for z in range(top, pool[0] - 1, -1):
        if z not in tiles_of:
            break
        t = tiles_of[z]
        if picked and (total + t > tile_budget or len(picked) >= depth):
            break
        picked.append(z)
        total += t
    picked.reverse()

    return {
        "levels": rows,
        "recommended": picked,
        "recommended_tiles": total,
        "budget_limited": budget_limited,
        "max_useful": useful[-1] if useful else z_max_cap,
        "min_useful": useful[0] if useful else picked[0],
    }


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
