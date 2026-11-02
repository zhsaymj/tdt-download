"""DEM 高程 GeoTIFF → Cesium quantized-mesh-1.0 地形切片(.terrain + layer.json)。

产出可被 CesiumJS `CesiumTerrainProvider` 直接加载的切片包:
  {out}/layer.json                最外层索引(瓦片可用区间 available)
  {out}/{L}/{x}/{y}.terrain       quantized-mesh-1.0 瓦片(未压缩)

技术链路(纯 Python,已实跑验证):
  1) 高程源统一到 EPSG:4326;按 Cesium 默认 GeographicTilingScheme 切网格。
  2) 逐瓦片 rasterio.warp.reproject 重采样出 (2^k+1)×(2^k+1) 高程窗口(默认 257)。
  3) pymartini.Martini(257).create_tile(grid).get_mesh(max_error) → TIN。
  4) pymartini.rescale_positions(顶点, grid, bounds, flip_y=True) → (lon,lat,height)。
  5) quantized_mesh_encoder.encode(positions, triangles, bounds) → .terrain 字节
     (encode 内部自动把经纬高转 ECEF、按 bounds 归一化,无需手工组装)。

Cesium 默认 GeographicTilingScheme(EPSG:4326)网格约定(易错点):
  - level 0 有 2×1 两张根瓦片,覆盖全球 [-180,-90,180,90]。
  - 第 L 级:列数 2^(L+1)、行数 2^L,单张瓦片经纬跨度 span = 180/2^L。
  - x 自西向东(0 在 -180°);y 自南向北(0 在 -90°,TMS 式)。
    => 与 osm.py/tms.py 不同:geodetic y 本就自南向北,range 算出的 y
       直接就是 .terrain 行号,不需要再翻转。
  - 级号 L 由输出想要的地理分辨率决定,与输入瓦片的 z 无固定 z-1 关系
    (输入 DEM 走墨卡托 XYZ,重投影到 4326 后按此网格独立定级)。
"""
from __future__ import annotations

import io
import math
import os
import struct
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.vrt import WarpedVRT
from rasterio.windows import from_bounds as window_from_bounds

from pymartini import Martini, rescale_positions
import quantized_mesh_encoder as qme

# pymartini 要求 2^k+1 的正方形网格
GRID = 257
# 缺省 TIN 简化误差(米):越小越精细、瓦片越大
DEFAULT_MAX_ERROR = 5.0

#: 低层级全球兜底的最大级号 —— 不加这层,地球是残缺的。
#:
#: Cesium 只渲染"确实有瓦片"的区域:bbox 之外没有瓦片,那片球面就没有地形。
#: 实测全国任务 L0 只出了 1 张瓦片(available 里 startX=endX=1),西半球整片
#: 无地形,地球看起来缺了一块。
#:
#: L <= 该值的层级按全球范围出瓦片。源范围外的瓦片由零高程规则网格填充,
#: 不读磁盘。更高层级仍只出 bbox 内 —— 远处粗、近处精,正是地形 LOD 要的样子。
#:
#: 取 6:L6 单张跨度 2.8125°,被 Cesium 升采样到更高层级后地球背面已是平滑
#: 球面;再往上是纯量级(全球 L7 就要 3.3 万张、L8 要 13 万张)而肉眼几乎
#: 看不出差别。
GLOBAL_COVER_LEVEL = 6

#: 全球兜底瓦片的网格密度(度/格) —— 决定椭球轮廓有多圆。
#:
#: 这条是被实测逼出来的:早先让兜底瓦片走 Martini,而 Martini 的简化判据基于
#: 高度差,零高程网格每点误差都是 0,任何 max_error(含 0)都会把它塌成 2 个
#: 三角形、4 个顶点。一个跨 180° 的 L0 瓦片于是成了 4 个角点连出的平面弦,
#: 整个地球渲染出来是个多面体。
#:
#: 所以兜底层级改走"规则网格 TIN"(见 _encode_rule_tile),不做简化,顶点直接
#: 落在椭球面上。每格 1.5° 时弦高偏差约 0.5km(地球半径 6371km),任意缩放下
#: 轮廓都是光滑球面。
_FLAT_SPAN_DEG = 1.5


def coverage_bbox_for_level(
    bbox: tuple[float, float, float, float],
    L: int,
    global_cover_level: int = GLOBAL_COVER_LEVEL,
) -> tuple[float, float, float, float]:
    """第 L 级实际要输出的瓦片范围:低层级全球兜底,高层级只出 bbox 内。

    global_cover_level < 0 时完全关闭兜底(退化为只出 bbox,即旧行为)。
    """
    if L <= global_cover_level:
        return (-180.0, -90.0, 180.0, 90.0)
    return bbox


def geodetic_span(L: int) -> float:
    """第 L 级单张瓦片的经纬跨度(度)。"""
    return 180.0 / (2 ** L)


def geodetic_tile_bounds(x: int, y: int, L: int) -> tuple[float, float, float, float]:
    """geodetic 网格瓦片 (x,y,L) 的 4326 地理范围 (west, south, east, north)。

    x 自西向东(0 在 -180°),y 自南向北(0 在 -90°)。
    """
    span = geodetic_span(L)
    west = -180.0 + x * span
    south = -90.0 + y * span
    return (west, south, west + span, south + span)


def geodetic_range_for_bbox(
    bbox: tuple[float, float, float, float], L: int
) -> tuple[int, int, int, int]:
    """bbox(west,south,east,north)在第 L 级覆盖的瓦片区间 (x_min,x_max,y_min,y_max)。

    y 已是自南向北的最终行号(无需翻转)。区间做级内边界钳制。
    """
    west, south, east, north = bbox
    span = geodetic_span(L)
    n_x = 2 ** (L + 1)
    n_y = 2 ** L
    x_min = int((west + 180.0) // span)
    x_max = int((east + 180.0) // span)
    y_min = int((south + 90.0) // span)
    y_max = int((north + 90.0) // span)
    # east/north 落在瓦片边界上时 // 会多算一格,收敛到闭区间
    if x_max >= n_x:
        x_max = n_x - 1
    if y_max >= n_y:
        y_max = n_y - 1
    x_min = max(0, min(x_min, n_x - 1))
    y_min = max(0, min(y_min, n_y - 1))
    return (x_min, x_max, y_min, y_max)


def level_for_resolution(deg_per_px: float) -> int:
    """由 4326 源分辨率(度/像素)推出最接近的 geodetic 级号。

    瓦片像素度数 = span/256 = 180/2^L/256,令其≈源 deg/px 反解 L。
    """
    if deg_per_px <= 0:
        return 0
    L = round(math.log2(180.0 / 256.0 / deg_per_px))
    return max(0, int(L))


def _read_tile_grid(
    vrt,
    vrt_bounds,
    bounds: tuple[float, float, float, float],
    grid: int = GRID,
) -> np.ndarray:
    """从 4326 高程源(WarpedVRT)窗口读出某瓦片的 grid×grid 高程窗口(float32)。

    只读瓦片覆盖的那一小块并重采样到 grid×grid(低层级自动走 overview),
    不再把整幅源喂给 reproject。WarpedVRT 不支持 boundless 读:与源范围求交,
    读交集区重采样后按像素偏移贴进 grid 画布(其余留 0=海平面)。
    nodata / 非有限值 / 明显异常低值统一填 0,避免 TIN 出尖刺或空洞。
    """
    west, south, east, north = bounds
    dst = np.zeros((grid, grid), dtype=np.float32)
    ix0, iy0 = max(west, vrt_bounds.left), max(south, vrt_bounds.bottom)
    ix1, iy1 = min(east, vrt_bounds.right), min(north, vrt_bounds.top)
    if ix1 <= ix0 or iy1 <= iy0:
        return dst   # 瓦片完全在源外:全 0
    span_x = east - west
    span_y = north - south
    # 交集区在 grid 画布内的像素范围(x 向东、y 向下:行 0 在北)
    px0 = int(round((ix0 - west) / span_x * grid))
    px1 = int(round((ix1 - west) / span_x * grid))
    py0 = int(round((north - iy1) / span_y * grid))
    py1 = int(round((north - iy0) / span_y * grid))
    ow, oh = px1 - px0, py1 - py0
    if ow <= 0 or oh <= 0:
        return dst
    window = window_from_bounds(ix0, iy0, ix1, iy1, vrt.transform)
    sub = vrt.read(1, out_shape=(oh, ow), window=window,
                   resampling=Resampling.bilinear).astype(np.float32)
    dst[py0:py1, px0:px1] = sub
    dst = np.where(np.isfinite(dst), dst, 0.0).astype(np.float32)
    # DEM_NODATA(-32768)及其它明显异常低值→0
    dst[dst < -1000.0] = 0.0
    return np.ascontiguousarray(dst)


def _encode_terrain(
    grid: np.ndarray,
    bounds: tuple[float, float, float, float],
    max_error: float,
) -> bytes:
    """grid 高程窗口 → quantized-mesh-1.0 瓦片字节(未压缩)。

    bounds: 瓦片 4326 范围 (west, south, east, north)。
    """
    west, south, east, north = bounds
    martini = Martini(grid.shape[0])
    tile = martini.create_tile(grid)
    vertices, triangles = tile.get_mesh(max_error)
    # (col,row) 顶点 → (lon,lat,height);flip_y=True 因数组行 0 在北、瓦片 y 自南向北
    positions = rescale_positions(
        vertices, grid, bounds=(west, south, east, north), flip_y=True
    )
    buf = io.BytesIO()
    qme.encode(buf, positions, triangles, bounds=(west, south, east, north))
    return buf.getvalue()


def _rule_grid_tin(n: int) -> np.ndarray:
    """n×n 规则网格的三角形索引。

    绕序必须与 Martini 一致(从椭球外看逆时针),否则整片地形会被背面剔除、
    完全看不见。实测该写法 2046/2048 个三角形法线朝外,反过来写 0/2048。
    """
    tris = []
    for r in range(n - 1):
        row = r * n
        nxt = row + n
        for c in range(n - 1):
            tl = row + c
            tr = tl + 1
            bl = nxt + c
            br = bl + 1
            tris.append((tl, bl, tr))
            tris.append((tr, bl, br))
    return np.asarray(tris, dtype=np.uint32)


def _encode_rule_tile(heights: np.ndarray,
                      bounds: tuple[float, float, float, float]) -> bytes:
    """规则网格 → quantized-mesh 瓦片,**不做简化**(这是与 _encode_terrain 的分别)。

    顶点按 (lon, lat, height) 铺满瓦片范围,行 0 在北(与
    rescale_positions(flip_y=True) 的约定一致)。全 0 的 heights 得到的就是一张
    贴着椭球面、网格致密的零高程瓦片 —— 全球兜底要的正是这个。
    """
    n = heights.shape[0]
    west, south, east, north = bounds
    lon2d, lat2d = np.meshgrid(np.linspace(west, east, n),
                               np.linspace(north, south, n))
    positions = np.stack([
        lon2d.ravel(),
        lat2d.ravel(),
        heights.astype(np.float64).ravel(),
    ], axis=1)
    buf = io.BytesIO()
    qme.encode(buf, positions, _rule_grid_tin(n),
               bounds=(west, south, east, north))
    return buf.getvalue()


def _tile_vertex_count(path: Path) -> int:
    """读 quantized-mesh 头里的顶点数(offset 88,见编码器 HEADER 布局)。

    头 88B = 中心点 24 + 最小/最大高程 8 + 包围球 32 + 地平遮挡点 24,
    紧跟 uint32 vertexCount。读不到就返回 0(当作需要重切)。
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(92)
    except OSError:
        return 0
    if len(head) < 92:
        return 0
    return struct.unpack("<I", head[88:92])[0]


def _flat_grid_for_span(span_deg: float) -> int:
    """按瓦片经纬跨度选规则网格边长,使每格不超过 _FLAT_SPAN_DEG 度。

    边长取 2^k+1 档(与 Martini 的网格约定统一,便于复用 _read_tile_grid)。
    """
    need = max(2, int(math.ceil(span_deg / _FLAT_SPAN_DEG)))
    for n in (3, 5, 9, 17, 33, 65, 129, 257):
        if n - 1 >= need:
            return n
    return 257


def _ensure_overviews(path: Path) -> None:
    """确保高程源有 overview 金字塔(缺则就地补建)。

    terrain 切片用 WarpedVRT 窗口读,低层级(L=0/1)单张瓦片覆盖范围极大,
    若源无 overview,GDAL 会把整幅全分辨率 DEM 全盘扫一遍再降采样到 257×257,
    每张几十秒、累加卡死。补建 overview 后低层级直接走降采样层,秒级出图。
    """
    try:
        with rasterio.open(path) as ds:
            if ds.overviews(1):   # 已有 overview
                return
        with rasterio.open(path, "r+") as ds:
            ds.build_overviews([2, 4, 8, 16, 32], Resampling.average)
            ds.update_tags(ns="rio_overview", resampling="average")
    except Exception:
        # 补建失败不阻断切片(仅退化为慢路径),避免影响成果产出
        pass


def export_terrain(
    dem_4326_path: Path,
    levels: list[int],
    bbox: tuple[float, float, float, float],
    out_dir: Path,
    on_progress=None,
    max_error: float = DEFAULT_MAX_ERROR,
    grid: int = GRID,
    should_stop=None,
    concurrency: int | None = None,
    global_cover_level: int = GLOBAL_COVER_LEVEL,
) -> tuple[Path, list[int], bool]:
    """把 4326 高程 GeoTIFF 切成 Cesium quantized-mesh 地形瓦片(多线程)。

    dem_4326_path: 高程源(必须已是 EPSG:4326 单波段 float32)。
    levels: 要输出的 geodetic 级号(L)列表。
    bbox: 经纬度矩形 (west, south, east, north),限定输出瓦片范围。
    global_cover_level: L <= 该值的层级按全球出瓦片(范围外填 0=海平面),
        保证 Cesium 里整个地球都有地形;<0 关闭(只出 bbox 内,旧行为)。
    on_progress(done_tiles, total_tiles): 细粒度上报已切瓦片数。
    should_stop(): 可选,返回 True 时在层/列边界尽快停止(暂停/取消)。
    concurrency: 切片线程数;None 时取 min(CPU 核数, 8)。
    高程用 WarpedVRT 窗口读(不再一次性读全图),每线程各建各的 vrt。
    返回 (out_dir, 已导出的级号列表, stopped)。写完后需调 write_layer_json 补索引。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stopped = False

    # 确保源有 overview 金字塔:WarpedVRT 窗口读靠 overview 快速降采样出低层级
    # 瓦片(L=0/1 单张覆盖极大范围)。缺 overview 时每张低层级瓦片都要全盘扫一遍
    # 多 GB 全分辨率 DEM,会慢到像卡死。此处一次性建好(幂等,已存在则很快)。
    _ensure_overviews(dem_4326_path)

    # 先取源 4326 有效四至(覆盖判断用)并校验坐标系
    with rasterio.open(dem_4326_path) as _ds0:
        if str(_ds0.crs) not in ("EPSG:4326",):
            raise ValueError(f"terrain 源必须为 EPSG:4326,实际 {_ds0.crs}")
        with WarpedVRT(_ds0, crs="EPSG:4326", resampling=Resampling.bilinear,
                       src_nodata=_ds0.nodata, nodata=0) as _v0:
            vb = _v0.bounds

    sorted_levels = sorted(levels)
    # 细粒度进度分母:各级瓦片数之和(高层级瓦片数最多,故按瓦片计更平滑)
    total_tiles = 0
    for L in sorted_levels:
        x_min, x_max, y_min, y_max = geodetic_range_for_bbox(
            coverage_bbox_for_level(bbox, L, global_cover_level), L)
        total_tiles += (x_max - x_min + 1) * (y_max - y_min + 1)
    total_tiles = max(total_tiles, 1)

    workers = concurrency if concurrency and concurrency > 0 else min(os.cpu_count() or 4, 8)

    # 每线程各建各的 rasterio.open + WarpedVRT(非线程安全,不跨线程共享);
    # 用 threading.local 懒创建,注册表登记、阶段末统一关闭防句柄泄漏。
    _tls = threading.local()
    _open_handles: list[tuple] = []
    lock = threading.Lock()
    done_ref = [0]
    exported_set: set[int] = set()

    def _get_vrt():
        v = getattr(_tls, "vrt", None)
        if v is None:
            ds = rasterio.open(dem_4326_path)
            v = WarpedVRT(ds, crs="EPSG:4326", resampling=Resampling.bilinear,
                          src_nodata=ds.nodata, nodata=0)
            _tls.ds = ds
            _tls.vrt = v
            with lock:
                _open_handles.append((ds, v))
        return _tls.vrt

    def _bump():
        with lock:
            done_ref[0] += 1
            d = done_ref[0]
        if on_progress and d % 8 == 0:
            on_progress(d, total_tiles)

    def _worker(x, y, L, dst):
        """线程任务:窗口读高程 → Martini TIN → 编码 → 写 .terrain。"""
        tb = geodetic_tile_bounds(x, y, L)
        west, south, east, north = tb
        if L <= global_cover_level:
            # 全球兜底层级:规则网格、不简化。既保证椭球轮廓圆滑,也顺带
            # 保住了落在这些层级上的真实地形(低层级本来也没几处)。
            n = _flat_grid_for_span(east - west)
            if (east <= vb.left or west >= vb.right
                    or north <= vb.bottom or south >= vb.top):
                # 完全在源外:零高程,连源句柄都不开
                g = np.zeros((n, n), dtype=np.float32)
            else:
                g = _read_tile_grid(_get_vrt(), vb, tb, n)
            raw = _encode_rule_tile(g, tb)
        else:
            g = _read_tile_grid(_get_vrt(), vb, tb, grid)
            raw = _encode_terrain(g, tb, max_error)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(raw)
        _bump()
        return L

    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for L in sorted_levels:
                if should_stop and should_stop():
                    stopped = True
                    break
                x_min, x_max, y_min, y_max = geodetic_range_for_bbox(
                    coverage_bbox_for_level(bbox, L, global_cover_level), L)
                futures = []
                submitted = 0
                for x in range(x_min, x_max + 1):
                    if should_stop and should_stop():
                        stopped = True
                        break
                    for y in range(y_min, y_max + 1):
                        dst = out_dir / str(L) / str(x) / f"{y}.terrain"
                        # 断点续切:已存在且非空的瓦片直接跳过(只计进度)。
                        # 兜底层级额外校验网格密度:旧版本把零高程瓦片简化成了
                        # 4 顶点平面(地球呈多面体),不重切就永远修不好。
                        if dst.exists() and dst.stat().st_size > 0:
                            if (L > global_cover_level
                                    or _tile_vertex_count(dst)
                                    >= _flat_grid_for_span(geodetic_span(L)) ** 2):
                                exported_set.add(L)
                                _bump()
                                continue
                        futures.append(pool.submit(_worker, x, y, L, dst))
                        submitted += 1
                for fut in futures:
                    r = fut.result()
                    if r is not None:
                        exported_set.add(r)
                if stopped:
                    break
    finally:
        for ds, v in _open_handles:
            try:
                v.close(); ds.close()
            except Exception:
                pass

    exported = sorted(exported_set)
    if on_progress and not stopped:
        on_progress(total_tiles, total_tiles)
    return out_dir, exported, stopped


def write_layer_json(
    out_dir: Path,
    bbox: tuple[float, float, float, float],
    levels: list[int],
    global_cover_level: int = GLOBAL_COVER_LEVEL,
) -> Path:
    """写最外层 layer.json(quantized-mesh-1.0 规范)。

    available 必须与实际出的瓦片一致:低层级声明为全球区间(见
    GLOBAL_COVER_LEVEL),否则 Cesium 不会去请求那些瓦片,兜底就白做了。
    y 已是自南向北 TMS 行号。未声明 octvertexnormals/watermask(首版不写法线)。
    """
    levels = sorted(levels)
    available = []
    for L in range(0, (max(levels) + 1) if levels else 0):
        if L in levels:
            x_min, x_max, y_min, y_max = geodetic_range_for_bbox(
                coverage_bbox_for_level(bbox, L, global_cover_level), L)
            available.append([{
                "startX": x_min, "startY": y_min,
                "endX": x_max, "endY": y_max,
            }])
        else:
            # 该级未切,但 available 数组下标必须与级号对齐,占位空区间
            available.append([])

    layer = {
        "tilejson": "2.1.0",
        "name": "terrain",
        "description": "",
        "version": "1.0.0",
        "format": "quantized-mesh-1.0",
        "attribution": "",
        "schema": "tms",
        "tiles": ["{z}/{x}/{y}.terrain"],
        "projection": "EPSG:4326",
        "bounds": [-180, -90, 180, 90],
        "available": available,
    }
    import json
    path = out_dir / "layer.json"
    path.write_text(json.dumps(layer, ensure_ascii=False, indent=2),
                    encoding="utf-8")
    return path
