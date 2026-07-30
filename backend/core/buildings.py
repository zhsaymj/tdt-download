"""建筑轮廓清洗、高度补全与底面高(DEM)采样。

三件事:
1. **轮廓清洗**:修复自相交、过滤过小面、丢弃退化环。
2. **高度补全**:Overture 的 height 缺失率在中国区偏高(多来自微软 ML 提取的
   无高度轮廓),按 height → num_floors×3m → 按占地面积分档估层 → 兜底 的
   顺序补齐,并统计各来源占比写入 metadata,让使用者知道成果里多少是真高度。
3. **底面高**:b3dm 是绝对定位几何,顶点高程写死在几何里,Cesium 加载
   Cesium3DTileset **不会**自动贴地形。而 Overture 只有 2D 轮廓 + 相对高度,
   不含海拔,故底面海拔必须在生成阶段烘焙。三种模式见 BaseHeightMode。

   terrain 模式采样策略:一栋楼取"质心 + 外接框四角"共 5 点的**中位数**,
   比只取质心稳(能避开 DEM 空洞与悬崖边缘异常值);同一栋楼底面恒为单一
   水平高度,不逐顶点贴地——否则楼会歪。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from ..providers.buildings import BuildingFeature
from .logs import logger

# ---------- 高度补全参数 ----------

#: 单层默认层高(米)
FLOOR_HEIGHT = 3.0
#: 高度全缺失时的兜底建筑高(米)
DEFAULT_HEIGHT = 6.0
#: 合理高度区间(米):超出视为脏数据,按缺失处理
HEIGHT_MIN, HEIGHT_MAX = 1.0, 1000.0

#: 按占地面积(㎡)分档估算层数:(面积下限, 层数)。自大到小匹配。
#: 依据是大占地建筑多为厂房/商业综合体(层数少),小占地多为住宅楼(层数多)。
AREA_FLOOR_TABLE = [
    (5000.0, 2),      # 大型厂房/仓库/航站楼:多为单层高大空间
    (1500.0, 3),      # 商业综合体/学校
    (400.0, 4),       # 多层住宅/办公
    (120.0, 5),       # 密集城区住宅
    (0.0, 2),         # 零散小房
]

#: 过滤阈值:占地小于此面积(㎡)的轮廓丢弃(多为附属棚屋/噪声)
MIN_AREA_M2 = 4.0

# ---------- 底面高模式 ----------


class BaseHeightMode:
    #: 采样 DEM 逐栋取地面海拔(默认,推荐;中国东西部高差极大,唯一全域可用)
    TERRAIN = "terrain"
    #: 底面固定 0(椭球高)。仅适用于不加载地形的纯白模预览
    FLAT = "flat"
    #: 全体统一偏移量。仅小范围平坦地形应急使用
    OFFSET = "offset"

    ALL = (TERRAIN, FLAT, OFFSET)


@dataclass
class CleanStats:
    """清洗与补全统计(写入 metadata,便于核对成果质量)。"""
    total_in: int = 0
    kept: int = 0
    dropped_small: int = 0
    dropped_invalid: int = 0
    h_from_height: int = 0
    h_from_floors: int = 0
    h_from_area: int = 0
    h_from_default: int = 0
    dem_sampled: int = 0
    dem_nodata: int = 0
    #: 各高程源实际命中栋数(上传地形 / 在线地形分别多少),便于判断覆盖情况
    dem_source_hits: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "input_features": self.total_in,
            "kept": self.kept,
            "dropped_too_small": self.dropped_small,
            "dropped_invalid_geometry": self.dropped_invalid,
            "height_sources": {
                "from_height_attr": self.h_from_height,
                "from_num_floors": self.h_from_floors,
                "from_area_estimate": self.h_from_area,
                "from_default": self.h_from_default,
            },
            "base_height": {
                "dem_sampled": self.dem_sampled,
                "dem_nodata_fallback": self.dem_nodata,
                # 例:{"上传地形": 812, "在线地形": 96} —— 说明有 96 栋落在
                # 上传地形之外、由在线地形兜底
                "dem_source_hits": dict(self.dem_source_hits or {}),
            },
        }


# ---------- 轮廓清洗 ----------

def _ring_area_m2(ring: list[tuple[float, float]]) -> float:
    """经纬度环的近似面积(㎡)。

    按环中心纬度做等距圆柱近似:1° 纬 ≈ 111320m,1° 经 ≈ 111320·cos(lat)m。
    建筑尺度(几十米)下误差可忽略,远快于投影计算。
    """
    if len(ring) < 3:
        return 0.0
    lat0 = sum(p[1] for p in ring) / len(ring)
    kx = 111320.0 * max(np.cos(np.radians(lat0)), 1e-6)
    ky = 111320.0
    # 鞋带公式
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0] * kx, ring[i][1] * ky
        x2, y2 = ring[(i + 1) % n][0] * kx, ring[(i + 1) % n][1] * ky
        s += x1 * y2 - x2 * y1
    return abs(s) * 0.5


def _clean_rings(rings: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]] | None:
    """清洗坐标环:去重复相邻点、丢弃退化环、必要时用 shapely 修复自相交。

    返回清洗后的 [外环, 洞...],不可用返回 None。
    """
    from shapely.geometry import Polygon

    def dedup(ring):
        out = []
        for p in ring:
            if not out or (abs(p[0] - out[-1][0]) > 1e-12 or abs(p[1] - out[-1][1]) > 1e-12):
                out.append(p)
        # 去掉与首点重合的末点
        if len(out) >= 2 and abs(out[0][0] - out[-1][0]) < 1e-12 and abs(out[0][1] - out[-1][1]) < 1e-12:
            out.pop()
        return out

    outer = dedup(rings[0])
    if len(outer) < 3:
        return None
    holes = [h for h in (dedup(r) for r in rings[1:]) if len(h) >= 3]

    poly = Polygon(outer, holes)
    if poly.is_valid:
        return [outer, *holes]

    # 自相交等:buffer(0) 是修复无效多边形的标准手法
    try:
        fixed = poly.buffer(0)
    except Exception:
        return None
    if fixed.is_empty:
        return None
    # 修复后可能变 MultiPolygon,取面积最大的那块
    if fixed.geom_type == "MultiPolygon":
        fixed = max(fixed.geoms, key=lambda g: g.area)
    if fixed.geom_type != "Polygon" or fixed.is_empty:
        return None

    def ring_of(seq):
        c = [(float(x), float(y)) for x, y in seq.coords]
        if len(c) >= 2 and c[0] == c[-1]:
            c = c[:-1]
        return c

    new_outer = ring_of(fixed.exterior)
    if len(new_outer) < 3:
        return None
    return [new_outer, *[r for r in (ring_of(i) for i in fixed.interiors) if len(r) >= 3]]


def _num(v) -> float | None:
    """把可能是字符串的数值字段安全转成 float,不可解析返回 None。

    数据源理应在自己那层把标签解析成数值,但这里再兜一道:任何一个源漏了
    解析都会让整条管线抛 TypeError(如 "8" > 0),代价远大于这几行。
    """
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _resolve_height(feat: BuildingFeature, area_m2: float,
                    default_height: float, stats: CleanStats) -> tuple[float, str]:
    """补全建筑高度,返回 (高度米, 来源标记)。"""
    h = _num(feat.height)
    if h is not None and HEIGHT_MIN <= h <= HEIGHT_MAX:
        stats.h_from_height += 1
        return h, "height"

    floors = _num(feat.num_floors)
    if floors and floors > 0:
        h2 = floors * FLOOR_HEIGHT
        if HEIGHT_MIN <= h2 <= HEIGHT_MAX:
            stats.h_from_floors += 1
            return h2, "num_floors"

    # 按占地面积分档估层
    for area_min, floors in AREA_FLOOR_TABLE:
        if area_m2 >= area_min:
            stats.h_from_area += 1
            return floors * FLOOR_HEIGHT, "area_estimate"

    stats.h_from_default += 1
    return float(default_height), "default"


# ---------- DEM 采样 ----------

class DemSampler:
    """从高程 GeoTIFF 采样地面海拔。

    DEM 由现有地形管线产出(EPSG:3857 单波段 float32,见 core/dem.py),
    这里按经纬度点采样。整幅 DEM 不载入内存,用 rasterio 的 sample 随机读。

    高程基准说明:DEM 是相对大地水准面的正高,Cesium 顶点用椭球高,两者相差
    大地水准面差距(中国境内约 -10~-50m)。此处**不做基准转换**——只要建筑底面
    与用户加载的 terrain 切片来自同一份 DEM,两者基准自洽、严丝合缝贴合;
    做半套转换反而会引入偏差。
    """

    def __init__(self, dem_path: Path):
        import rasterio
        from rasterio.warp import transform as _warp_transform

        self._rio = rasterio
        self._warp = _warp_transform
        self.ds = rasterio.open(dem_path)
        self.nodata = self.ds.nodata
        self._is_4326 = str(self.ds.crs).upper().endswith("4326")
        logger.info("DEM 采样源:%s crs=%s size=%dx%d",
                    Path(dem_path).name, self.ds.crs, self.ds.width, self.ds.height)

    def close(self):
        try:
            self.ds.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def sample_many(self, lonlats: list[tuple[float, float]]) -> list[float | None]:
        """批量采样。返回与输入等长的高程列表,无效值为 None。"""
        if not lonlats:
            return []
        lons = [p[0] for p in lonlats]
        lats = [p[1] for p in lonlats]
        if self._is_4326:
            xs, ys = lons, lats
        else:
            xs, ys = self._warp("EPSG:4326", self.ds.crs, lons, lats)
        out: list[float | None] = []
        for val in self.ds.sample(zip(xs, ys), indexes=1):
            v = float(val[0])
            if self.nodata is not None and abs(v - float(self.nodata)) < 1e-6:
                out.append(None)
            elif not np.isfinite(v) or v < -12000.0 or v > 9000.0:
                out.append(None)      # 明显越界:当作无效
            else:
                out.append(v)
        return out

    def base_height_of(self, rings) -> float | None:
        """一栋建筑的底面海拔:质心 + 外接框四角共 5 点的中位数。

        单点采样容易落进 DEM 空洞或悬崖像元;取 5 点中位数显著更稳。
        全部无效返回 None(交由上层决定回退到别的高程源还是用偏移值)。
        """
        outer = rings[0]
        xs = [p[0] for p in outer]
        ys = [p[1] for p in outer]
        cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
        minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
        pts = [(cx, cy), (minx, miny), (minx, maxy), (maxx, miny), (maxx, maxy)]
        vals = [v for v in self.sample_many(pts) if v is not None]
        if not vals:
            return None
        return float(np.median(vals))


class MultiDemSampler:
    """按优先级从多个高程源采样,前者取不到才用后者。

    场景:用户上传了本地地形(更精细),但它可能只覆盖下载范围的一部分。
    那就把上传地形放第一优先、在线地形放兜底——范围内用精细数据,范围外
    仍有值可用,不会因为地形没盖全而丢建筑。

    注意两种源的高程基准可能不同(上传成果多为正高,在线 DEM 也是正高但
    模型不同),交界处可能出现台阶。这是数据本身的差异,工具只做如实拼接,
    并在统计里分别计数,便于事后判断影响范围。
    """

    def __init__(self, paths_with_label):
        """paths_with_label: [(Path, 标签), ...],按优先级从高到低。"""
        self.samplers: list[tuple[DemSampler, str]] = []
        for p, label in paths_with_label:
            if p is None:
                continue
            try:
                self.samplers.append((DemSampler(Path(p)), label))
            except Exception as e:
                logger.warning("高程源 %s 打开失败,跳过:%s", label, str(e)[:160])
        if not self.samplers:
            raise RuntimeError("没有可用的高程源")
        #: 各源实际命中次数(写入统计,便于判断上传地形覆盖了多少)
        self.hits: dict[str, int] = {label: 0 for _s, label in self.samplers}

    def close(self):
        for s, _l in self.samplers:
            s.close()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()

    def base_height_of(self, rings) -> float | None:
        for s, label in self.samplers:
            v = s.base_height_of(rings)
            if v is not None:
                self.hits[label] = self.hits.get(label, 0) + 1
                return v
        return None


# ---------- 主流程 ----------

def prepare_buildings(
    features,
    *,
    base_mode: str = BaseHeightMode.TERRAIN,
    dem_path: Path | None = None,
    dem_sources=None,
    height_offset: float = 0.0,
    default_height: float = DEFAULT_HEIGHT,
    min_area_m2: float = MIN_AREA_M2,
    on_progress=None,
    should_stop=None,
):
    """清洗 + 补全高度 + 定底面高,产出可直接建模的建筑列表。

    features: BuildingFeature 可迭代(来自 Building3DSource.fetch)。
    返回 (buildings: list[BuildingFeature], stats: CleanStats)。

    base_mode=terrain 时需要高程源,二者给其一:
      dem_sources: [(路径, 标签), ...] 按优先级从高到低(如上传地形优先、在线兜底)
      dem_path   : 单个高程源(等价于只有一个源)
    高程全部采不到的建筑回退 height_offset,并计入 stats.dem_nodata
    (不丢弃——宁可有个近似高度也别丢建筑)。
    """
    if base_mode not in BaseHeightMode.ALL:
        raise ValueError(f"未知底面高模式:{base_mode}")

    stats = CleanStats()
    out: list[BuildingFeature] = []
    sampler = None

    if base_mode == BaseHeightMode.TERRAIN:
        srcs = list(dem_sources or [])
        if not srcs and dem_path:
            srcs = [(dem_path, "dem")]
        srcs = [(p, l) for p, l in srcs if p and Path(p).exists()]
        if not srcs:
            raise RuntimeError(
                "底面高模式为 terrain 但缺少高程源,无法采样地面海拔")
        sampler = MultiDemSampler(srcs)
        logger.info("底面高采样源(按优先级):%s",
                    " → ".join(l for _p, l in srcs))

    try:
        for feat in features:
            if should_stop and should_stop():
                logger.info("建筑清洗被中断,已处理 %d 栋", stats.total_in)
                break
            stats.total_in += 1

            rings = _clean_rings(feat.rings)
            if not rings:
                stats.dropped_invalid += 1
                continue

            area = _ring_area_m2(rings[0])
            if area < min_area_m2:
                stats.dropped_small += 1
                continue

            height, src = _resolve_height(feat, area, default_height, stats)

            if sampler is not None:
                base = sampler.base_height_of(rings)
                if base is None:
                    stats.dem_nodata += 1
                    base = float(height_offset)
                else:
                    stats.dem_sampled += 1
                    base += float(height_offset)   # 允许在采样值上再叠加微调
            elif base_mode == BaseHeightMode.OFFSET:
                base = float(height_offset)
            else:
                base = 0.0

            feat.rings = rings
            feat.height = height
            feat.height_source = src
            feat.base_height = float(base)
            # 合并而非覆盖:props 里可能已有数据源带来的用户自选保留字段
            # (本地矢量上传场景),直接赋值会把它们全部丢掉。
            feat.props = {**(feat.props or {}), "area_m2": round(area, 2)}
            out.append(feat)
            stats.kept += 1

            if on_progress and stats.kept % 500 == 0:
                on_progress(stats.kept, None)
    finally:
        if sampler is not None:
            # 先取命中统计再关闭
            stats.dem_source_hits = dict(getattr(sampler, "hits", {}) or {})
            sampler.close()

    logger.info(
        "建筑清洗完成:输入 %d,保留 %d(过滤 小面积%d/无效%d);"
        "高度来源 属性%d/层数%d/面积估%d/兜底%d;底面 DEM采样%d/回退%d",
        stats.total_in, stats.kept, stats.dropped_small, stats.dropped_invalid,
        stats.h_from_height, stats.h_from_floors, stats.h_from_area,
        stats.h_from_default, stats.dem_sampled, stats.dem_nodata,
    )
    if stats.dem_source_hits:
        logger.info("底面高各源命中:%s",
                    ",".join(f"{k} {v} 栋" for k, v in stats.dem_source_hits.items()))
    return out, stats
