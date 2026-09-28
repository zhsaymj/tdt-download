# Google / Esri 影像数据源 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增 Google 影像(4 图层)与 Esri World Imagery 两个 EPSG:3857 墨卡托影像数据源,支持经代理下载、拼接 GeoTIFF、四种格式导出与工具内底图预览。

**Architecture:** 引入"网格"维度(`geodetic`/`mercator`)区分两套瓦片数学;抽出 `core/mercator_tiling.py` 作为 3857 网格的唯一实现;下载器加 session 级代理与 404 语义分流;预览端点在主进程独立限流转发。对 `core/scheduler.py` 与 `core/worker.py` **零改动** —— 新 provider 的 kind 是 `RASTER_IMAGE`,自然路由到现有 `runner.run_task`。

**Tech Stack:** Python 3.11、aiohttp 3.11.11、rasterio 1.4.4、标准库 unittest、FastAPI、Vue3 + OpenLayers。

**设计文档:** `docs/superpowers/specs/2026-09-24-google-esri影像数据源-design.md`(下称"设计 §N")

---

## 关键前提(动手前必读)

1. **代理是硬前提。** Google 与 Esri World Imagery 直连均不通(设计 §3.1 复验:3/3 TimeoutError)。开发机需运行本地代理,本计划按 `127.0.0.1:6789` 编写。
2. **`ClientSession(proxy=...)` 必须带 scheme。** 传 `"127.0.0.1:6789"` 会抛 `InvalidURL`(设计 §3.7 实测)。这是 Task 3 的核心。
3. **不设单任务瓦片数上限。** 用户已决定(设计 §9 Q1)。**不要**引入 `MERCATOR_TILE_LIMIT`,Task 17 有回归护栏盯着这件事。
4. **Esri 上限 z19**(用户已决定,设计 §9 Q4),且实际可用级别**随地区变化**(西部无人区仅到 z17),必须按区域探测(Task 15)。
5. **测试夹具已就绪。** `tests/fixtures/` 下 5 个实抓瓦片,含关键负样本 `esri_wi_real_ocean_678B.jpg`。**不要重新构造测试数据。**

**运行测试的命令**(Windows,项目根目录下):

```bash
.venv/Scripts/python.exe -m unittest tests.test_xxx -v      # 单个文件
.venv/Scripts/python.exe -m unittest discover tests         # 全量(约 160+ 用例)
```

**实施分阶段**(与设计 §7 一致):

- **阶段一(Task 1-4)**:网格数学抽取 + 拼接加 crs + 代理归一化 + 404 分流。纯重构与基础能力,现有测试须全绿。
- **阶段二(Task 5-10)**:两个 provider + 配置 + formats 登记 + runner 分流。可下载、可拼 GeoTIFF。
- **阶段三(Task 11-17)**:区域级别探测 + 代理预检 + 建议级别 + 估算分流。提交路径安全。
- **阶段四(Task 18-19)**:四种导出格式按网格分流。
- **阶段五(Task 18-21)**:预览端点 + 前端接入 + 裁剪修复 + 端到端验收。

---

## 文件结构

**新增文件:**

| 文件 | 职责 |
|---|---|
| `backend/core/mercator_tiling.py` | EPSG:3857 网格数学的唯一实现(换算、区间、四至、估算、建议级别) |
| `backend/providers/google.py` | Google 4 图层 provider |
| `backend/providers/esri_imagery.py` | Esri World Imagery provider + 区域最高级别探测 |
| `backend/api/tiles.py` | 预览瓦片转发端点(主进程,独立限流) |
| `tests/test_mercator_tiling.py` | 网格数学 + 与 `osm.py` 原实现逐点一致的回归护栏 |
| `tests/test_proxy_normalize.py` | 代理串归一化 |
| `tests/test_missing_status.py` | 404 语义分流(不重试、不计失败) |
| `tests/test_google_provider.py` | Google provider |
| `tests/test_esri_imagery_provider.py` | Esri provider + `is_empty_tile` 两级判定 + 探测 |
| `tests/test_mosaic_crs.py` | 拼接 crs 参数 + 默认行为向后兼容 |
| `tests/test_formats_grid.py` | 网格登记表 |
| `tests/test_mercator_suggest.py` | 建议级别 + `suggest_dem_levels` 不回归 |
| `tests/test_estimate_mercator.py` | 估算分流 + **大范围不被拒绝**的回归护栏 |
| `tests/test_preview_tiles.py` | 预览端点(SSRF 护栏、降级) |

**修改文件:**

| 文件 | 改动 |
|---|---|
| `backend/providers/base.py` | 加 `proxy` 属性、`missing_statuses()`、`normalize_proxy()` |
| `backend/core/downloader.py` | session 级 `proxy`、404 分流 |
| `backend/core/mosaic.py` | `mosaic_to_geotiff` 加 `crs` 参数 |
| `backend/core/dem_tiling.py` | 改为从 `mercator_tiling` re-export |
| `backend/core/osm.py` | 删除本地重复的 XYZ 换算,改 import |
| `backend/core/formats.py` | 加 `PROVIDER_GRID` / `grid_of()`,`PROVIDER_KIND` 加 5 行 |
| `backend/core/runner.py` | provider 构造分支、按网格分流瓦片区间与拼接 |
| `backend/config.py` | `GoogleConfig` / `EsriImageryConfig` + `_CONFIG_TEMPLATE` |
| `backend/main.py` | 挂载 tiles 路由、lifespan 起/关预览 session |
| `backend/api/tasks.py` | 估算/建议级别分流、代理预检、Esri 级别剔除 |
| `backend/api/tools.py` | 代理连通性诊断 |
| `config.example.yaml` | 补两节配置 |
| `frontendvue/src/utils/basemap.js` | 加 Google/Esri 底图项 |
| `frontendvue/src/composables/useMap.js` | `setBasemap` 按 provider 分派 |
| `frontendvue/src/utils/taskDefaults.js` | 加 mercator 级别常量 |
| `frontendvue/src/components/ProcessDialog.vue` | 数据源下拉 + 级别上限 + Esri 探测 |
| `frontendvue/src/components/RedownloadDialog.vue` | 数据源下拉 |
| `frontendvue/src/components/TaskDetail.vue` | 显示名映射 |
| `frontendvue/src/api.js` | 加 `imageryMaxLevel` |

---

# 阶段一:网格数学与下载器基础能力

本阶段是纯重构 + 新增基础能力,**不引入任何新数据源**。验收标准:现有全量测试保持全绿,行为无变化。

## Task 1: 抽出 `core/mercator_tiling.py`

现状 `core/dem_tiling.py:22-42` 与 `core/osm.py:54-76` 各有一份**完全相同**的 XYZ 换算实现。不抽出就要写第三份。

**Files:**
- Create: `backend/core/mercator_tiling.py`
- Create: `tests/test_mercator_tiling.py`
- Modify: `backend/core/dem_tiling.py`
- Modify: `backend/core/osm.py`

- [x] **Step 1: 写失败测试**

创建 `tests/test_mercator_tiling.py`:

```python
"""EPSG:3857 墨卡托 XYZ 网格数学。

重点是两条回归护栏:
  1. 与 dem_tiling / osm 原有实现逐点一致(本模块是从它们抽出来的)
  2. dem_tiling 的旧名字仍可导入(避免破坏现有调用)
"""
import math
import unittest

from backend.core.mercator_tiling import (
    LAT_LIMIT, MERC_MAX, TILE_SIZE, estimate_mercator_tiles, lonlat_to_xyz,
    mercator_range_for_bbox, mosaic_bounds_3857, tile_bounds_3857,
)


class TestLonlatToXyz(unittest.TestCase):
    def test_origin_top_left(self):
        """z=1 时左上象限是 (0, 0)。"""
        self.assertEqual(lonlat_to_xyz(-179.0, 80.0, 1), (0, 0))

    def test_z0_single_tile(self):
        self.assertEqual(lonlat_to_xyz(0.0, 0.0, 0), (0, 0))

    def test_beijing_z16(self):
        """北京 (116.386, 40.0) 在 z16 的瓦片号(实测基准值)。"""
        self.assertEqual(lonlat_to_xyz(116.386, 40.0, 16), (53955, 24810))

    def test_lat_clamped_to_limit(self):
        """超出墨卡托纬度上限时夹紧,不应抛异常或产生越界瓦片号。"""
        x, y = lonlat_to_xyz(0.0, 89.9, 5)
        self.assertGreaterEqual(y, 0)
        self.assertEqual(y, lonlat_to_xyz(0.0, LAT_LIMIT, 5)[1])

    def test_clamped_within_matrix(self):
        """经度 180 边界不应越界到 2^z。"""
        n = 2 ** 4
        x, y = lonlat_to_xyz(180.0, 0.0, 4)
        self.assertLess(x, n)


class TestTileBounds(unittest.TestCase):
    def test_z0_covers_world(self):
        minx, miny, maxx, maxy = tile_bounds_3857(0, 0, 0)
        self.assertAlmostEqual(minx, -MERC_MAX, places=6)
        self.assertAlmostEqual(maxx, MERC_MAX, places=6)
        self.assertAlmostEqual(maxy, MERC_MAX, places=6)
        self.assertAlmostEqual(miny, -MERC_MAX, places=6)

    def test_roundtrip_lonlat_to_tile_to_bounds(self):
        """经纬度 -> 瓦片 -> 四至:原点应落在该瓦片范围内。"""
        lon, lat, z = 116.386, 40.0, 14
        x, y = lonlat_to_xyz(lon, lat, z)
        minx, miny, maxx, maxy = tile_bounds_3857(x, y, z)
        mx = MERC_MAX * lon / 180.0
        my = MERC_MAX * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2)) / math.pi
        self.assertTrue(minx <= mx <= maxx)
        self.assertTrue(miny <= my <= maxy)


class TestRangeForBbox(unittest.TestCase):
    def test_single_tile_range(self):
        tr = mercator_range_for_bbox(116.386, 40.000, 116.3861, 40.0001, 10)
        self.assertEqual(tr.count, 1)
        self.assertEqual(tr.z, 10)

    def test_known_counts(self):
        """0.05 度选区的逐级瓦片数(设计 §3.8 实测基准)。"""
        bbox = (116.36, 39.98, 116.41, 40.03)
        self.assertEqual(mercator_range_for_bbox(*bbox, 18).count, 1862)
        self.assertEqual(mercator_range_for_bbox(*bbox, 19).count, 7104)

    def test_mosaic_bounds_matches_corner_tiles(self):
        tr = mercator_range_for_bbox(116.36, 39.98, 116.41, 40.03, 12)
        minx, miny, maxx, maxy = mosaic_bounds_3857(tr)
        tl = tile_bounds_3857(tr.col_min, tr.row_min, tr.z)
        br = tile_bounds_3857(tr.col_max, tr.row_max, tr.z)
        self.assertAlmostEqual(minx, tl[0], places=6)
        self.assertAlmostEqual(maxy, tl[3], places=6)
        self.assertAlmostEqual(maxx, br[2], places=6)
        self.assertAlmostEqual(miny, br[1], places=6)


class TestEstimate(unittest.TestCase):
    def test_sums_per_level(self):
        bbox = (116.36, 39.98, 116.41, 40.03)
        got = estimate_mercator_tiles(bbox, [18, 19])
        self.assertEqual(got, 1862 + 7104)

    def test_empty_levels(self):
        self.assertEqual(estimate_mercator_tiles((0, 0, 1, 1), []), 0)


class TestBackwardCompat(unittest.TestCase):
    """回归护栏:抽出后旧调用路径必须不变。"""

    def test_dem_tiling_reexports(self):
        from backend.core import dem_tiling
        self.assertIs(dem_tiling.lonlat_to_xyz, lonlat_to_xyz)
        self.assertIs(dem_tiling.mercator_range_for_bbox, mercator_range_for_bbox)
        self.assertIs(dem_tiling.mosaic_bounds_3857, mosaic_bounds_3857)
        self.assertIs(dem_tiling.tile_bounds_3857, tile_bounds_3857)

    def test_dem_tiling_old_estimate_name_kept(self):
        """estimate_dem_tiles 是 api/tasks.py 正在用的名字,不能消失。"""
        from backend.core.dem_tiling import estimate_dem_tiles
        bbox = (116.36, 39.98, 116.41, 40.03)
        self.assertEqual(estimate_dem_tiles(bbox, [18]),
                         estimate_mercator_tiles(bbox, [18]))

    def test_osm_uses_shared_impl(self):
        """osm.py 不应再有自己的一份实现。"""
        from backend.core import osm
        self.assertIs(osm.lonlat_to_xyz, lonlat_to_xyz)
        self.assertIs(osm.tile_bounds_3857, tile_bounds_3857)

    def test_constants_agree(self):
        self.assertEqual(TILE_SIZE, 256)
        self.assertAlmostEqual(MERC_MAX, 20037508.342789244, places=6)
        self.assertAlmostEqual(LAT_LIMIT, 85.05112878, places=8)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_mercator_tiling -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.mercator_tiling'`

- [x] **Step 3: 创建 `backend/core/mercator_tiling.py`**

把 `dem_tiling.py` 的纯数学函数搬过来(签名保持不变),内容如下:

```python
"""EPSG:3857 Web 墨卡托 XYZ 瓦片网格数学 —— 3857 网格的唯一实现。

天地图用 EPSG:4326(core/tiling.py);本模块服务所有墨卡托 XYZ 数据源:
Esri Terrain3D DEM、Google 影像、Esri World Imagery,以及 OSM/XYZ 导出。

抽出的原因:dem_tiling 与 osm 原先各有一份完全相同的实现,再加数据源就是
第三份。两者现在都从这里 import,不再各自维护。

XYZ 约定:x 自西向东(0 在 -180°),y 自北向南(0 在顶部),每级 2^z × 2^z 张。
"""
from __future__ import annotations

import math

from .tiling import TileRange

TILE_SIZE = 256
# Web 墨卡托世界范围半边长(米)
MERC_MAX = 20037508.342789244
# Web 墨卡托纬度上限(度)
LAT_LIMIT = 85.05112878


def lonlat_to_xyz(lon: float, lat: float, z: int) -> tuple[int, int]:
    """经纬度 → XYZ 瓦片行列 (x, y)。"""
    lat = max(-LAT_LIMIT, min(LAT_LIMIT, lat))
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    lat_rad = math.radians(lat)
    y = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    x = max(0, min(n - 1, x))
    y = max(0, min(n - 1, y))
    return x, y


def tile_bounds_3857(x: int, y: int, z: int) -> tuple[float, float, float, float]:
    """XYZ 瓦片在 EPSG:3857 下的地理范围 (minx, miny, maxx, maxy),单位米。"""
    n = 2 ** z
    span = 2 * MERC_MAX / n
    minx = -MERC_MAX + x * span
    maxx = minx + span
    maxy = MERC_MAX - y * span
    miny = maxy - span
    return minx, miny, maxx, maxy


def mercator_range_for_bbox(
    west: float, south: float, east: float, north: float, z: int
) -> TileRange:
    """给定经纬度矩形范围与级别,计算覆盖的墨卡托 XYZ 瓦片区间。

    复用 TileRange(col=x, row=y),供下载器与拼接使用。
    """
    x0, y0 = lonlat_to_xyz(west, north, z)   # 左上
    x1, y1 = lonlat_to_xyz(east, south, z)   # 右下
    col_min, col_max = min(x0, x1), max(x0, x1)
    row_min, row_max = min(y0, y1), max(y0, y1)
    return TileRange(z, col_min, col_max, row_min, row_max)


def mosaic_bounds_3857(tr: TileRange) -> tuple[float, float, float, float]:
    """整个 XYZ 瓦片区间拼接后的 3857 地理四至 (minx, miny, maxx, maxy)。"""
    minx, _, _, maxy = tile_bounds_3857(tr.col_min, tr.row_min, tr.z)  # 左上瓦片
    _, miny, maxx, _ = tile_bounds_3857(tr.col_max, tr.row_max, tr.z)  # 右下瓦片
    return minx, miny, maxx, maxy


def estimate_mercator_tiles(
    bbox: tuple[float, float, float, float], levels: list[int]
) -> int:
    """按选中级别估算墨卡托瓦片总数。

    刻意不设上限、不做拦截:调用方(api 层)只负责如实呈现规模,
    是否下载由用户决定(设计 §4.10 / §9 Q1)。
    """
    w, s, e, n = bbox
    return sum(mercator_range_for_bbox(w, s, e, n, z).count for z in levels)


def _tile_xyz_range(bbox, z: int):
    """给定经纬度 bbox 与级别,返回覆盖的 (x 列表, y 列表)。

    osm.py 的切片循环用它;与 mercator_range_for_bbox 等价,
    只是返回 range 对象而非 TileRange。
    """
    west, south, east, north = bbox
    x0, y0 = lonlat_to_xyz(west, north, z)   # 左上
    x1, y1 = lonlat_to_xyz(east, south, z)   # 右下
    xs = range(min(x0, x1), max(x0, x1) + 1)
    ys = range(min(y0, y1), max(y0, y1) + 1)
    return xs, ys
```

- [x] **Step 4: 改 `backend/core/dem_tiling.py` 为 re-export**

删除该文件中的 `TILE_SIZE` / `MERC_MAX` / `LAT_LIMIT` 常量定义、`lonlat_to_xyz`、`tile_bounds_3857`、`mercator_range_for_bbox`、`mosaic_bounds_3857`、`estimate_dem_tiles` 这些定义(连同 `import math`),把文件顶部改成:

```python
"""DEM 专属的级别建议逻辑。

3857 网格数学已抽到 core/mercator_tiling.py(Google/Esri 影像也用它);
本模块只保留 DEM 特有的 suggest_dem_levels,其余名字从那里 re-export
以免破坏现有调用(api/tasks.py、core/runner.py、providers/terrain.py)。
"""
from __future__ import annotations

from .mercator_tiling import (  # noqa: F401  (re-export,保持旧导入路径可用)
    LAT_LIMIT, MERC_MAX, TILE_SIZE, lonlat_to_xyz, mercator_range_for_bbox,
    mosaic_bounds_3857, tile_bounds_3857,
)
from .mercator_tiling import estimate_mercator_tiles

#: 旧名字:api/tasks.py 正在用。保留别名而非改调用点,减少本次改动面。
estimate_dem_tiles = estimate_mercator_tiles
```

`suggest_dem_levels` 函数体**原样保留**(它内部调用 `mercator_range_for_bbox` 与 `mosaic_bounds_3857`,现在来自 re-export,无需改动)。

- [x] **Step 5: 改 `backend/core/osm.py` 删除重复实现**

删除 `osm.py` 中的 `TILE_SIZE` / `MERC_MAX` / `LAT_LIMIT` 常量、`lonlat_to_xyz`、`tile_bounds_3857`、`_tile_xyz_range` 四个定义(约在 30-84 行),并删除已无用的 `import math`。在 `from .tile_clip import ...` 之前加:

```python
from .mercator_tiling import (
    LAT_LIMIT, MERC_MAX, TILE_SIZE, _tile_xyz_range, lonlat_to_xyz,
    tile_bounds_3857,
)
```

> 注意 `osm.py` 内部多处用到 `TILE_SIZE`,import 后它们无需改动。

- [x] **Step 6: 运行新测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_mercator_tiling -v`
Expected: PASS,15 项通过

- [x] **Step 7: 运行全量测试确认重构无回归**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 与改动前同样数量的用例通过,无新增失败。特别确认 `test_scheduler`、`test_worker_dispatch`、任何涉及 DEM 与 OSM 导出的用例保持绿。

- [x] **Step 8: 提交**

```bash
git add backend/core/mercator_tiling.py backend/core/dem_tiling.py backend/core/osm.py tests/test_mercator_tiling.py
git commit -m "refactor(tiling): 抽出 mercator_tiling 作为 3857 网格数学唯一实现

dem_tiling 与 osm 原先各有一份相同的 XYZ 换算,再加影像数据源就是第三份。
两者改为从新模块 import;dem_tiling 保留 estimate_dem_tiles 别名以免破坏
api/tasks.py 的现有调用。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: `mosaic_to_geotiff` 加 `crs` 参数

`core/mosaic.py:95` 硬编码 `"crs": "EPSG:4326"`,且 bounds 来自 `tr.mosaic_bounds()`(4326 专用)。参数化约 5 行,默认值保持 4326 完全向后兼容。

**Files:**
- Create: `tests/test_mosaic_crs.py`
- Modify: `backend/core/mosaic.py:65-105`

- [x] **Step 1: 写失败测试**

创建 `tests/test_mosaic_crs.py`:

```python
"""mosaic_to_geotiff 的 crs 参数。

护栏重点:默认参数下的输出必须与改动前完全一致(天地图现有行为不能变)。
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio

from backend.core.mercator_tiling import mercator_range_for_bbox, mosaic_bounds_3857
from backend.core.mosaic import mosaic_to_geotiff
from backend.core.tiling import range_for_bbox


class _FakeProvider:
    """最小 provider:拼接只用到 ext / bands / key。"""
    key = "fake_img"
    ext = "png"
    bands = 3


def _write_tile(path: Path, value: int) -> None:
    """写一张 256x256 的纯色 PNG 作为瓦片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((3, 256, 256), value, dtype=np.uint8)
    profile = {"driver": "PNG", "height": 256, "width": 256,
               "count": 3, "dtype": "uint8"}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(arr)


class TestMosaicCrs(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.provider = _FakeProvider()

    def tearDown(self):
        self._tmp.cleanup()

    def _tile_path_fn(self, cache: Path):
        def fn(col, row, z):
            return cache / str(z) / f"{col}_{row}.png"
        return fn

    def _prepare(self, tr, cache: Path):
        for col in range(tr.col_min, tr.col_max + 1):
            for row in range(tr.row_min, tr.row_max + 1):
                _write_tile(cache / str(tr.z) / f"{col}_{row}.png", 128)

    def test_default_is_4326(self):
        """不传 crs 时行为不变:EPSG:4326 + tr.mosaic_bounds()。"""
        cache = self.tmp / "cache"
        tr = range_for_bbox(116.36, 39.98, 116.41, 40.03, 10)
        self._prepare(tr, cache)
        out = self.tmp / "out_default.tif"
        mosaic_to_geotiff(self.provider, cache, tr, out,
                          self._tile_path_fn(cache))
        with rasterio.open(out) as ds:
            self.assertEqual(ds.crs.to_string(), "EPSG:4326")
            west, south, east, north = tr.mosaic_bounds()
            self.assertAlmostEqual(ds.bounds.left, west, places=6)
            self.assertAlmostEqual(ds.bounds.top, north, places=6)
            self.assertAlmostEqual(ds.bounds.right, east, places=6)
            self.assertAlmostEqual(ds.bounds.bottom, south, places=6)

    def test_explicit_3857_uses_mercator_bounds(self):
        """crs='EPSG:3857' 时 bounds 来自 mosaic_bounds_3857。"""
        cache = self.tmp / "cache3857"
        tr = mercator_range_for_bbox(116.36, 39.98, 116.41, 40.03, 12)
        self._prepare(tr, cache)
        out = self.tmp / "out_3857.tif"
        mosaic_to_geotiff(self.provider, cache, tr, out,
                          self._tile_path_fn(cache), crs="EPSG:3857")
        with rasterio.open(out) as ds:
            self.assertEqual(ds.crs.to_string(), "EPSG:3857")
            minx, miny, maxx, maxy = mosaic_bounds_3857(tr)
            self.assertAlmostEqual(ds.bounds.left, minx, places=3)
            self.assertAlmostEqual(ds.bounds.top, maxy, places=3)
            self.assertAlmostEqual(ds.bounds.right, maxx, places=3)
            self.assertAlmostEqual(ds.bounds.bottom, miny, places=3)

    def test_explicit_4326_same_as_default(self):
        """显式传 EPSG:4326 与不传应产出相同的地理参考。"""
        cache = self.tmp / "cache_same"
        tr = range_for_bbox(116.36, 39.98, 116.41, 40.03, 10)
        self._prepare(tr, cache)
        a = self.tmp / "a.tif"
        b = self.tmp / "b.tif"
        mosaic_to_geotiff(self.provider, cache, tr, a, self._tile_path_fn(cache))
        mosaic_to_geotiff(self.provider, cache, tr, b,
                          self._tile_path_fn(cache), crs="EPSG:4326")
        with rasterio.open(a) as da, rasterio.open(b) as db:
            self.assertEqual(da.crs, db.crs)
            self.assertEqual(da.transform, db.transform)
            self.assertEqual(da.shape, db.shape)

    def test_3857_pixel_size_is_square(self):
        """3857 下像素应近似正方(墨卡托等角),这是坐标算对的旁证。"""
        cache = self.tmp / "cache_sq"
        tr = mercator_range_for_bbox(116.36, 39.98, 116.41, 40.03, 12)
        self._prepare(tr, cache)
        out = self.tmp / "sq.tif"
        mosaic_to_geotiff(self.provider, cache, tr, out,
                          self._tile_path_fn(cache), crs="EPSG:3857")
        with rasterio.open(out) as ds:
            self.assertAlmostEqual(abs(ds.transform.a), abs(ds.transform.e),
                                   places=6)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_mosaic_crs -v`
Expected: FAIL — `test_explicit_3857_uses_mercator_bounds` 报 `TypeError: mosaic_to_geotiff() got an unexpected keyword argument 'crs'`

- [x] **Step 3: 改 `backend/core/mosaic.py`**

把函数签名(65-73 行)改为:

```python
def mosaic_to_geotiff(
    provider: TileProvider,
    cache_dir: Path,
    tr: TileRange,
    out_path: Path,
    tile_path_fn,
    anno_tile_path_fn=None,
    on_row=None,
    crs: str = "EPSG:4326",
) -> Path:
```

在 docstring 末尾(`on_row` 说明之后)补一行:

```
    crs: 输出坐标系。默认 EPSG:4326(天地图网格);墨卡托数据源
      (Google/Esri 影像)传 "EPSG:3857",bounds 改从 mosaic_bounds_3857 取。
```

把第 85 行的 bounds 取值改为:

```python
    # bounds 的来源随网格而变:4326 用 TileRange 自带的方法,3857 需用
    # 墨卡托四至(单位米)。二者不能混用 —— 混了会写出坐标系与坐标值
    # 不匹配的 GeoTIFF(QGIS 里表现为图在南极洲外面)。
    if crs == "EPSG:3857":
        from .mercator_tiling import mosaic_bounds_3857
        west, south, east, north = mosaic_bounds_3857(tr)
    else:
        west, south, east, north = tr.mosaic_bounds()
    transform = from_bounds(west, south, east, north, width, height)
```

把 profile 里的第 95 行 `"crs": "EPSG:4326",` 改为:

```python
        "crs": crs,
```

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_mosaic_crs -v`
Expected: PASS,4 项通过

- [x] **Step 5: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿。默认参数未变,现有天地图与 DEM 拼接用例应不受影响。

- [x] **Step 6: 提交**

```bash
git add backend/core/mosaic.py tests/test_mosaic_crs.py
git commit -m "feat(mosaic): mosaic_to_geotiff 支持 crs 参数,为 3857 影像拼接铺路

默认值保持 EPSG:4326,现有天地图调用行为完全不变。传 EPSG:3857 时
bounds 改从 mosaic_bounds_3857 取(单位米),避免坐标系与坐标值不匹配。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 代理归一化与 provider 的 `proxy` / `missing_statuses`

**这是设计里唯一的实测纠错点。** `ClientSession(proxy="127.0.0.1:6789")` 会抛 `InvalidURL` —— aiohttp 要求带 scheme(设计 §3.7)。

**Files:**
- Create: `tests/test_proxy_normalize.py`
- Modify: `backend/providers/base.py`

- [x] **Step 1: 写失败测试**

创建 `tests/test_proxy_normalize.py`:

```python
"""代理串归一化。

背景:aiohttp 的 proxy 参数必须是带 scheme 的完整 URL,传 "127.0.0.1:6789"
会抛 InvalidURL(实测 aiohttp 3.11.11)。而用户在 config.yaml 里习惯只写
host:port(buildings.proxy 现有配置就是这个形式),故代码侧补 scheme。
"""
import unittest

from backend.providers.base import TileProvider, normalize_proxy


class TestNormalizeProxy(unittest.TestCase):
    def test_bare_host_port_gets_http_scheme(self):
        """核心用例:用户按习惯只写 host:port。"""
        self.assertEqual(normalize_proxy("127.0.0.1:6789"),
                         "http://127.0.0.1:6789")

    def test_existing_scheme_kept(self):
        self.assertEqual(normalize_proxy("http://127.0.0.1:6789"),
                         "http://127.0.0.1:6789")

    def test_https_scheme_kept(self):
        self.assertEqual(normalize_proxy("https://proxy.local:8443"),
                         "https://proxy.local:8443")

    def test_socks5_passed_through_unchanged(self):
        """不静默降级成 http —— 那会连到 SOCKS 端口发 HTTP 请求,报错极难懂。"""
        self.assertEqual(normalize_proxy("socks5://127.0.0.1:1080"),
                         "socks5://127.0.0.1:1080")

    def test_empty_is_none(self):
        self.assertIsNone(normalize_proxy(""))

    def test_none_is_none(self):
        self.assertIsNone(normalize_proxy(None))

    def test_whitespace_only_is_none(self):
        self.assertIsNone(normalize_proxy("   "))

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(normalize_proxy("  127.0.0.1:6789  "),
                         "http://127.0.0.1:6789")

    def test_hostname_without_port(self):
        self.assertEqual(normalize_proxy("proxy.local"),
                         "http://proxy.local")


class _MinimalProvider(TileProvider):
    key = "minimal"

    def tile_url(self, col, row, z):
        return "https://example.com/tile"


class TestProviderDefaults(unittest.TestCase):
    def test_proxy_defaults_to_none(self):
        """现有数据源(天地图/DEM)不走代理,默认必须是 None。"""
        self.assertIsNone(_MinimalProvider().proxy)

    def test_missing_statuses_defaults_empty(self):
        """默认空集:天地图与 Esri 用 200+内容表达无数据,不用状态码。"""
        self.assertEqual(_MinimalProvider().missing_statuses(), frozenset())

    def test_tianditu_provider_has_no_proxy(self):
        """回归护栏:不能让天地图被意外绕进代理。"""
        from backend.providers.tianditu import build_provider
        p = build_provider("tianditu_img", "dummy-token")
        self.assertIsNone(p.proxy)
        self.assertEqual(p.missing_statuses(), frozenset())

    def test_terrain_provider_has_no_proxy(self):
        """Esri Terrain3D 直连可用,同样不应走代理。"""
        from backend.providers.terrain import build_terrain_provider
        p = build_terrain_provider("esri_terrain")
        self.assertIsNone(p.proxy)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_proxy_normalize -v`
Expected: FAIL — `ImportError: cannot import name 'normalize_proxy' from 'backend.providers.base'`

- [x] **Step 3: 改 `backend/providers/base.py`**

在 `from abc import ABC, abstractmethod` 之后、`class TileProvider` 之前插入:

```python
def normalize_proxy(raw: str | None) -> str | None:
    """把配置里的代理串归一化为 aiohttp 可用的 URL;空值返回 None(直连)。

    aiohttp 的 proxy 参数必须是带 scheme 的完整 URL —— 传 "127.0.0.1:6789"
    会抛 InvalidURL(实测 3.11.11)。用户在 config.yaml 里习惯只写 host:port
    (buildings.proxy 现有配置就是这个形式),故这里补 scheme,而不是让用户改写法。

    socks5:// 原样返回但**不受支持**:aiohttp 不内置 SOCKS(需 aiohttp-socks)。
    这里刻意不静默降级成 http —— 那会连到 SOCKS 端口发 HTTP 请求,报出的错
    与真正病因毫无关系。原样传下去让 aiohttp 自己报 scheme 不支持,更好排查。

    放在 base 而非各 provider 内:worker 里的下载器与主进程里的预览端点
    都要用同一份归一化结果,复制两份必然漂移。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    if "://" in raw:
        return raw
    return f"http://{raw}"
```

在 `TileProvider` 类内、`headers` 属性之后插入两个成员:

```python
    @property
    def proxy(self) -> str | None:
        """下载该数据源瓦片时使用的 HTTP 代理 URL(默认无;子类可覆盖)。

        返回值必须已归一化(带 scheme),可直接传给 aiohttp —— 子类应当返回
        normalize_proxy(cfg.proxy) 而不是 cfg.proxy 本身。

        刻意做成 provider 属性而非全局开关:天地图与 Esri Terrain3D 直连可用,
        若用全局代理(或 aiohttp 的 trust_env),用户为别的软件设的系统代理会把
        它们也绕进去 —— 那是现有功能的静默回归。
        """
        return None

    def missing_statuses(self) -> frozenset[int]:
        """该数据源用这些 HTTP 状态码表示"此瓦片无数据"(默认空集)。

        与 is_empty_tile 的分工:后者判 200 响应的**内容**(占位图),
        本方法判**状态码**。Google 对无影像位置返回 404 + 标准错误页
        (实测,设计 §3.12);天地图与 Esri 都用 200 + 内容,故默认为空。

        下载器据此判定:不重试、不计失败、不写缓存(该处确实没有数据)。
        """
        return frozenset()
```

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_proxy_normalize -v`
Expected: PASS,13 项通过

- [x] **Step 5: 提交**

```bash
git add backend/providers/base.py tests/test_proxy_normalize.py
git commit -m "feat(providers): 加 proxy 属性、missing_statuses 与 normalize_proxy

normalize_proxy 补 scheme:aiohttp 的 proxy 参数要求完整 URL,直接传
配置里的 127.0.0.1:6789 会抛 InvalidURL(实测 3.11.11)。

代理做成 provider 属性而非全局开关,避免天地图/DEM 被意外绕进代理。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 下载器接入代理与 404 分流

**Files:**
- Create: `tests/test_missing_status.py`
- Modify: `backend/core/downloader.py:62-96`(session 构造)、`:107-157`(`_one` 状态判断)

- [x] **Step 1: 写失败测试**

创建 `tests/test_missing_status.py`:

```python
"""下载器的代理传递与 404 语义分流。

两件事:
  1. provider.proxy 必须传给 ClientSession(session 级,不是逐请求)
  2. missing_statuses 命中时:不重试、不计失败、不写缓存

为什么 404 不能重试:Google 对无影像位置返回 404(海洋/极地/无覆盖)。
一个纯海域的 0.05 度选区约 1800 张瓦片,按 max_retries=3 会白跑 5400 次请求。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.core.downloader import TileDownloader
from backend.core.tiling import TileRange
from backend.providers.base import TileProvider


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """记录每个 URL 被请求了几次,并按预设返回状态码。"""

    def __init__(self, status: int, body: bytes = b"xx"):
        self.status = status
        self.body = body
        self.calls: list[str] = []
        self.session_kwargs: dict = {}

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResponse(self.status, self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Missing404Provider(TileProvider):
    """模拟 Google:404 表示该瓦片无影像。"""
    key = "fake_google"
    ext = "jpg"
    bands = 3

    def tile_url(self, col, row, z):
        return f"https://example.com/{z}/{col}/{row}.jpg"

    def missing_statuses(self) -> frozenset[int]:
        return frozenset({404})

    @property
    def proxy(self) -> str | None:
        return "http://127.0.0.1:6789"


class _PlainProvider(TileProvider):
    """不声明 missing_statuses:404 按普通失败处理。"""
    key = "fake_plain"
    ext = "jpg"
    bands = 3

    def tile_url(self, col, row, z):
        return f"https://example.com/{z}/{col}/{row}.jpg"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class TestMissingStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self.tr = TileRange(10, 0, 0, 0, 0)     # 单张瓦片

    def tearDown(self):
        self._tmp.cleanup()

    def _download(self, provider, session, retries=3):
        dl = TileDownloader(provider, cache_dir=self.cache,
                            concurrency=2, max_retries=retries, timeout=5)
        import backend.core.downloader as mod
        orig = mod.aiohttp.ClientSession
        captured = {}

        def fake_client_session(**kwargs):
            captured.update(kwargs)
            return session

        mod.aiohttp.ClientSession = fake_client_session
        try:
            ok, fail, stopped = _run(dl.download_range(self.tr))
        finally:
            mod.aiohttp.ClientSession = orig
        return ok, fail, stopped, captured

    def test_404_counts_as_success_not_failure(self):
        """该处确实无数据,请求本身是成功的,不该计入 failed。"""
        session = _FakeSession(404)
        ok, fail, _stopped, _kw = self._download(_Missing404Provider(), session)
        self.assertEqual(ok, 1)
        self.assertEqual(fail, 0)

    def test_404_not_retried(self):
        """关键:只请求 1 次,而非 max_retries+1 次。"""
        session = _FakeSession(404)
        self._download(_Missing404Provider(), session, retries=3)
        self.assertEqual(len(session.calls), 1)

    def test_404_not_cached(self):
        """无数据不该落盘,否则断点续传会把它当有效缓存。"""
        session = _FakeSession(404)
        self._download(_Missing404Provider(), session)
        self.assertEqual(list(self.cache.rglob("*.jpg")), [])

    def test_404_without_declaration_is_failure(self):
        """未声明 missing_statuses 的数据源:404 仍按失败处理并重试。"""
        session = _FakeSession(404)
        ok, fail, _stopped, _kw = self._download(_PlainProvider(), session,
                                                 retries=2)
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 1)
        self.assertEqual(len(session.calls), 3)     # 首次 + 2 次重试

    def test_proxy_passed_to_session(self):
        """代理必须是 session 级:_one 里有重试循环,逐请求传容易漏。"""
        session = _FakeSession(404)
        _ok, _fail, _stopped, kwargs = self._download(_Missing404Provider(),
                                                      session)
        self.assertEqual(kwargs.get("proxy"), "http://127.0.0.1:6789")

    def test_no_proxy_when_provider_has_none(self):
        session = _FakeSession(404)
        _ok, _fail, _stopped, kwargs = self._download(_PlainProvider(), session)
        self.assertIsNone(kwargs.get("proxy"))

    def test_trust_env_not_enabled(self):
        """不能开 trust_env:会把天地图也绕进用户的系统代理(静默回归)。"""
        session = _FakeSession(404)
        _ok, _fail, _stopped, kwargs = self._download(_PlainProvider(), session)
        self.assertNotIn("trust_env", kwargs)

    def test_200_still_cached(self):
        """正常瓦片行为不变。"""
        session = _FakeSession(200, b"realbytes")
        ok, fail, _stopped, _kw = self._download(_Missing404Provider(), session)
        self.assertEqual((ok, fail), (1, 0))
        self.assertEqual(len(list(self.cache.rglob("*.jpg"))), 1)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_missing_status -v`
Expected: FAIL — `test_404_counts_as_success_not_failure` 等报 ok=0/fail=1(404 当前按失败处理);`test_proxy_passed_to_session` 报 `proxy` 不在 kwargs 里

- [x] **Step 3: 改 `backend/core/downloader.py` 的 session 构造**

把第 74 行的 `async with aiohttp.ClientSession(...)` 改为:

```python
        # 代理取自 provider(Google/Esri 需要;天地图与 DEM 直连)。
        # getattr 带默认值是为兼容 CachedBuildingSource 这类装饰器与测试替身。
        #
        # 必须用 session 级 proxy 而非逐请求传:_one 里有重试循环,逐请求要在
        # 每处 session.get 都带上,漏一处就是"重试时突然直连" —— 表现为偶发
        # 超时,极难定位。
        #
        # 刻意不设 trust_env=True:那会读 HTTP_PROXY 环境变量,把天地图/DEM
        # 也绕进用户为别的软件设的系统代理(现有功能的静默回归)。
        proxy = getattr(self.provider, "proxy", None)
        async with aiohttp.ClientSession(headers=headers, timeout=timeout,
                                         proxy=proxy) as session:
```

- [x] **Step 4: 改 `_one` 的状态判断**

在 `_one` 方法里,先在 `for attempt in ...` 循环之前取一次声明(避免每次重试都调):

```python
        # 该数据源用哪些状态码表示"此瓦片无数据"(Google 用 404)
        missing = getattr(self.provider, "missing_statuses", lambda: frozenset())()
```

然后把 `async with session.get(url) as resp:` 之后的状态判断改为:

```python
                    async with session.get(url) as resp:
                        if resp.status in missing:
                            # 该处确实无影像(海洋/极地/无覆盖),非瞬态故障。
                            # 不重试、不计失败、不写缓存 —— 与 is_empty_tile
                            # 的处理一致(请求本身是成功的)。
                            #
                            # 不重试是关键:海域范围大时这是绝大多数瓦片,按
                            # 原逻辑每张要白跑 max_retries 次 + 指数退避
                            # (一个纯海域 0.05 度选区约 1800 张 => 5400 次无效请求)。
                            return True
                        if resp.status == 200:
                            data = await resp.read()
                            if data:
```

(下方 `is_empty_tile` 与原子写那段逻辑**保持不动**,只是缩进层级不变。)

> ⚠️ 注意:原代码是 `if resp.status == 200:`,改动只是在它**之前**插入 missing 分支,不要改动 200 分支内部的任何逻辑。

- [x] **Step 5: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_missing_status -v`
Expected: PASS,8 项通过

- [x] **Step 6: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿。`missing_statuses` 默认空集,现有数据源行为不变。

- [x] **Step 7: 阶段一端到端验证:经代理取一张真实瓦片**

创建临时脚本 `scripts/_probe_proxy.py`(验证后删除):

```python
"""阶段一验收:确认经代理能取到 Google 瓦片,且 normalize_proxy 生效。

刻意用不带 scheme 的配置值 —— 这正是设计 §3.7 纠错的那个坑。
"""
import asyncio
import sys

import aiohttp

sys.path.insert(0, ".")
from backend.providers.base import normalize_proxy

RAW_PROXY = "127.0.0.1:6789"      # 不带 scheme,模拟用户配置


async def main():
    proxy = normalize_proxy(RAW_PROXY)
    print(f"归一化: {RAW_PROXY!r} -> {proxy!r}")
    timeout = aiohttp.ClientTimeout(total=20)
    url = "https://mt1.google.com/vt/lyrs=s&x=53955&y=24810&z=16"
    async with aiohttp.ClientSession(timeout=timeout, proxy=proxy) as s:
        async with s.get(url) as r:
            data = await r.read()
            print(f"status={r.status} size={len(data)} ct={r.headers.get('Content-Type')}")
            assert r.status == 200, "经代理仍取不到瓦片,检查代理是否运行"
            assert len(data) > 1000, "响应过小,可能不是瓦片"
    print("阶段一验收通过")


asyncio.run(main())
```

Run: `.venv/Scripts/python.exe scripts/_probe_proxy.py`
Expected:
```
归一化: '127.0.0.1:6789' -> 'http://127.0.0.1:6789'
status=200 size=18084 ct=image/jpeg
阶段一验收通过
```

然后删除该脚本:`rm scripts/_probe_proxy.py`

> 若这一步失败,**先解决**再继续 —— 阶段二会同时改 provider、配置、formats、runner 四处,代理问题混在里面排查面会放大。

- [x] **Step 8: 提交**

```bash
git add backend/core/downloader.py tests/test_missing_status.py
git commit -m "feat(downloader): session 级代理 + 404 无数据语义分流

代理用 session 级而非逐请求:_one 有重试循环,逐请求传漏一处就是
重试时突然直连(偶发超时,极难定位)。不开 trust_env 以免天地图被
意外绕进系统代理。

missing_statuses 命中的状态码(Google 的 404)视为该瓦片无数据:
不重试、不计失败、不写缓存。海域范围按原逻辑每张瓦片要白跑 3 次重试。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

# 阶段二:两个数据源可下载、可拼 GeoTIFF

## Task 5: 配置项

**Files:**
- Modify: `backend/config.py`
- Modify: `config.example.yaml`

- [x] **Step 1: 加两个 dataclass**

在 `backend/config.py` 的 `WorkerConfig` 之后插入:

```python
@dataclass
class GoogleConfig:
    """Google 影像(非官方瓦片端点)。

    实测直连不通(超时),**必须配代理**。端点忽略 key 参数(带/不带/空 key
    返回字节完全相同的瓦片),故刻意不设 key 字段,避免"填了 key 才有权限"的误解。
    """
    enabled: bool = False
    # HTTP 代理。可写 "127.0.0.1:6789" 或 "http://127.0.0.1:6789",
    # 代码会补 scheme(见 providers/base.normalize_proxy)。不支持 socks5。
    proxy: str = ""
    # 非官方端点会变更(实测旧 khms 端点 v=1000 已返回 404),故可配置。
    url_template: str = "https://mt{s}.google.com/vt/lyrs={lyrs}&x={x}&y={y}&z={z}"
    subdomains: str = "0,1,2,3"
    # 实测陆地处处可到 z21(含西部城市),z22 仅部分地区有 —— 21 是全球陆地
    # 可用的临界值。无地区性降级,故不需要按区域探测。
    max_zoom: int = 21

    def subdomain_list(self) -> list[str]:
        """归一化子域名配置为列表;未配置时回落默认。"""
        items = [x.strip() for x in str(self.subdomains or "").split(",")]
        return [x for x in items if x] or ["0", "1", "2", "3"]


@dataclass
class EsriImageryConfig:
    """Esri World Imagery。

    与项目现用的 Esri Terrain3D(DEM)是**不同服务**:Terrain3D 直连可用,
    World Imagery 实测直连不通,必须配代理。
    """
    enabled: bool = False
    proxy: str = ""
    url_template: str = ("https://services.arcgisonline.com/ArcGIS/rest/services"
                         "/World_Imagery/MapServer/tile/{z}/{y}/{x}")
    # 服务级天花板。实测 z19 是亚欧城市的实际上限(z20 仅美国境内有),
    # 且 z19 载有真实新增细节(高频能量比 z18 上采样高 33~46%),不是插值放大。
    max_zoom: int = 19
    # 是否按选区探测该地区的实际最高级别。默认开启:实测西藏/青海/新疆无人区
    # z18 即无影像(最高 z17),不探测的话用户选 z18 会下到一整片灰色占位图。
    probe_max_zoom: bool = True
    # 探测结果按量化 bbox 缓存的有效期(小时)。0 = 不缓存。
    # 探测是逐级网络请求,用户拖拽选区会连续触发,缓存不是优化而是必需。
    probe_cache_hours: float = 24.0
```

- [x] **Step 2: 注册到 `Config` 并加载**

在 `Config` 类的字段列表里(`worker` 之后)加:

```python
    google: GoogleConfig = field(default_factory=GoogleConfig)
    esri_imagery: EsriImageryConfig = field(default_factory=EsriImageryConfig)
```

在 `load_config()` 的 `_merge(cfg.worker, raw.get("worker"))` 之后加:

```python
        _merge(cfg.google, raw.get("google"))
        _merge(cfg.esri_imagery, raw.get("esri_imagery"))
```

在环境变量覆盖段(`env_workers` 那一块之后)加:

```python
    # 代理可用环境变量覆盖(便于临时切换而不改配置文件)
    env_gproxy = os.environ.get("GOOGLE_PROXY")
    if env_gproxy:
        cfg.google.proxy = env_gproxy
    env_eproxy = os.environ.get("ESRI_PROXY")
    if env_eproxy:
        cfg.esri_imagery.proxy = env_eproxy
```

- [x] **Step 3: 补 `_CONFIG_TEMPLATE`**

`_CONFIG_TEMPLATE` 是首次启动自动生成的模板,**必须同步** —— 只改 `config.example.yaml` 会让新用户拿到的 `config.yaml` 缺这两节。在模板字符串末尾(`py3dtiles_python` 那行之后)追加:

```yaml

google:
  # Google 影像(非官方瓦片端点)。实测直连不通,必须配代理。
  enabled: false
  # 代理地址。可写 "127.0.0.1:6789" 或带 scheme 的完整 URL,两种都行。
  # 注意:不支持 socks5(aiohttp 不内置 SOCKS 支持)。
  proxy: ""
  # 端点会变更,失效时先改这里。不设 key 字段:该端点忽略 key 参数。
  url_template: "https://mt{s}.google.com/vt/lyrs={lyrs}&x={x}&y={y}&z={z}"
  subdomains: "0,1,2,3"
  max_zoom: 21          # 实测陆地处处可用到 21;22 级仅部分地区有

esri_imagery:
  # Esri World Imagery。与现用的 Esri Terrain3D(DEM)是不同服务:
  # Terrain3D 直连可用,World Imagery 实测直连不通,必须配代理。
  enabled: false
  proxy: ""
  url_template: "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}"
  # 服务级天花板。实测 z19 是亚欧城市上限(z20 仅美国境内有)。
  max_zoom: 19
  # 按选区探测该地区实际最高级别。实测西部无人区最高仅 z17,
  # 关掉后用户选 z18 会下到一整片灰色占位图。
  probe_max_zoom: true
  probe_cache_hours: 24
```

- [x] **Step 4: 同步 `config.example.yaml`**

把 Step 3 的同一段 YAML 追加到 `config.example.yaml` 末尾。

- [x] **Step 5: 验证配置能加载**

Run:
```bash
.venv/Scripts/python.exe -c "
from backend.config import load_config
c = load_config()
print('google.enabled =', c.google.enabled)
print('google.max_zoom =', c.google.max_zoom)
print('google.subdomain_list() =', c.google.subdomain_list())
print('esri.max_zoom =', c.esri_imagery.max_zoom)
print('esri.probe_max_zoom =', c.esri_imagery.probe_max_zoom)
"
```
Expected:
```
google.enabled = False
google.max_zoom = 21
google.subdomain_list() = ['0', '1', '2', '3']
esri.max_zoom = 19
esri.probe_max_zoom = True
```

- [x] **Step 6: 提交**

```bash
git add backend/config.py config.example.yaml
git commit -m "feat(config): 加 google 与 esri_imagery 配置节

两个源都需代理(实测直连不通)。不设 key 字段:Google 非官方端点忽略
该参数。Esri max_zoom 定 19(亚欧城市实际上限),实际可用级别按区域探测。

_CONFIG_TEMPLATE 与 config.example.yaml 同步 —— 前者是首次启动自动
生成的模板,只改后者会让新用户的 config.yaml 缺这两节。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Google provider

**Files:**
- Create: `backend/providers/google.py`
- Create: `tests/test_google_provider.py`

- [x] **Step 1: 写失败测试**

创建 `tests/test_google_provider.py`:

```python
"""Google 影像 provider(4 图层,EPSG:3857 墨卡托 XYZ)。"""
import unittest

from backend.config import GoogleConfig
from backend.providers.google import (
    GOOGLE_LAYERS, build_google_provider, is_google_provider,
)


def _cfg(**kw) -> GoogleConfig:
    base = GoogleConfig(enabled=True, proxy="127.0.0.1:6789")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


class TestLayerRegistry(unittest.TestCase):
    def test_four_layers(self):
        self.assertEqual(set(GOOGLE_LAYERS), {
            "google_img", "google_hybrid", "google_road", "google_terrain"})

    def test_is_google_provider(self):
        self.assertTrue(is_google_provider("google_img"))
        self.assertTrue(is_google_provider("google_road"))
        self.assertFalse(is_google_provider("tianditu_img"))
        self.assertFalse(is_google_provider("esri_imagery"))
        self.assertFalse(is_google_provider(""))


class TestBands(unittest.TestCase):
    def test_satellite_is_three_bands_jpg(self):
        p = build_google_provider("google_img", _cfg())
        self.assertEqual(p.bands, 3)
        self.assertEqual(p.ext, "jpg")

    def test_hybrid_is_three_bands_jpg(self):
        p = build_google_provider("google_hybrid", _cfg())
        self.assertEqual(p.bands, 3)
        self.assertEqual(p.ext, "jpg")

    def test_terrain_is_three_bands_jpg(self):
        p = build_google_provider("google_terrain", _cfg())
        self.assertEqual(p.bands, 3)
        self.assertEqual(p.ext, "jpg")

    def test_road_is_single_band_png(self):
        """实测 lyrs=m 返回 count=1 的调色板 PNG。按 3 波段处理会抛
        DatasetIOShapeError,故必须登记为 1。"""
        p = build_google_provider("google_road", _cfg())
        self.assertEqual(p.bands, 1)
        self.assertEqual(p.ext, "png")


class TestTileUrl(unittest.TestCase):
    def test_lyrs_and_coords_substituted(self):
        p = build_google_provider("google_img", _cfg(subdomains="1"))
        url = p.tile_url(53955, 24810, 16)
        self.assertEqual(
            url, "https://mt1.google.com/vt/lyrs=s&x=53955&y=24810&z=16")

    def test_each_layer_uses_its_lyrs_code(self):
        want = {"google_img": "s", "google_hybrid": "y",
                "google_road": "m", "google_terrain": "p"}
        for key, code in want.items():
            p = build_google_provider(key, _cfg(subdomains="0"))
            self.assertIn(f"lyrs={code}&", p.tile_url(1, 2, 3))

    def test_subdomain_rotates(self):
        p = build_google_provider("google_img", _cfg(subdomains="0,1,2,3"))
        subs = [p.tile_url(1, 1, 5).split("//")[1].split(".")[0]
                for _ in range(5)]
        self.assertEqual(subs, ["mt0", "mt1", "mt2", "mt3", "mt0"])

    def test_custom_url_template_honored(self):
        """端点会变更,模板必须可配置。"""
        p = build_google_provider(
            "google_img",
            _cfg(url_template="https://alt{s}.example.com/t?l={lyrs}&{x}/{y}/{z}",
                 subdomains="9"))
        self.assertEqual(p.tile_url(7, 8, 9),
                         "https://alt9.example.com/t?l=s&7/8/9")

    def test_empty_url_template_rejected(self):
        with self.assertRaises(ValueError):
            build_google_provider("google_img", _cfg(url_template=""))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            build_google_provider("google_nope", _cfg())


class TestZoomAndProxy(unittest.TestCase):
    def test_max_zoom_from_config(self):
        self.assertEqual(build_google_provider("google_img", _cfg()).max_zoom(), 21)

    def test_max_zoom_configurable(self):
        p = build_google_provider("google_img", _cfg(max_zoom=19))
        self.assertEqual(p.max_zoom(), 19)

    def test_min_zoom_is_one(self):
        self.assertEqual(build_google_provider("google_img", _cfg()).min_zoom(), 1)

    def test_proxy_normalized(self):
        """配置里写 host:port,provider 必须返回带 scheme 的 URL。"""
        p = build_google_provider("google_img", _cfg(proxy="127.0.0.1:6789"))
        self.assertEqual(p.proxy, "http://127.0.0.1:6789")

    def test_proxy_empty_is_none(self):
        p = build_google_provider("google_img", _cfg(proxy=""))
        self.assertIsNone(p.proxy)


class TestEmptyTileSemantics(unittest.TestCase):
    def test_no_placeholder_behavior(self):
        """实测 Google 无 200 占位图:无影像处返回 404(见 missing_statuses)。"""
        p = build_google_provider("google_img", _cfg())
        self.assertFalse(p.is_empty_tile(b"\xff\xd8\xff\xe0anything"))
        self.assertFalse(p.is_empty_tile(b""))

    def test_404_declared_as_missing(self):
        p = build_google_provider("google_img", _cfg())
        self.assertEqual(p.missing_statuses(), frozenset({404}))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_google_provider -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.providers.google'`

- [x] **Step 3: 创建 `backend/providers/google.py`**

```python
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
```

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_google_provider -v`
Expected: PASS,18 项通过

- [x] **Step 5: 提交**

```bash
git add backend/providers/google.py tests/test_google_provider.py
git commit -m "feat(providers): 加 Google 影像 provider(4 图层)

google_road 的 bands 登记为 1:实测 lyrs=m 返回单波段调色板 PNG,
按 3 波段处理会抛 DatasetIOShapeError。

is_empty_tile 恒 False 是实测结论而非未实现:Google 无影像处返回
404 + 错误页,无数据识别走 missing_statuses。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Esri World Imagery provider 与占位图两级判定

**本任务是阶段二的重点。** `is_empty_tile` 的朴素实现会误伤真实数据 —— 一张 678 字节、唯一色值仅 11 个的**真实深海瓦片**会被"体量小 + 色值少"的规则打成"无数据"。

夹具已就绪(**不要重新构造测试数据**):

| 文件 | 字节 | mean | std | 唯一色值 | 期望 |
|---|---|---|---|---|---|
| `esri_wi_placeholder_2521B.jpg` | 2521 | 204.73 | 5.37 | 79 | True |
| `esri_wi_real_ocean_678B.jpg` | 678 | 19.99 | 14.96 | 11 | **False(关键)** |
| `esri_wi_real_sahara.jpg` | 3578 | 154.68 | 28.55 | 156 | False |
| `esri_wi_real_beijing.jpg` | 16368 | 131.17 | 46.94 | 254 | False |
| `esri_wi_real_tibet_z17.jpg` | 22828 | 120.58 | 62.94 | 256 | False |

**Files:**
- Create: `backend/providers/esri_imagery.py`
- Create: `tests/test_esri_imagery_provider.py`

- [x] **Step 1: 写失败测试**

创建 `tests/test_esri_imagery_provider.py`:

```python
"""Esri World Imagery provider 与占位图判定。

判定测试用的是**实抓的真实瓦片字节**(tests/fixtures/),不是构造的数据。
最重要的一条是 test_real_ocean_tile_not_empty:678 字节的真实深海瓦片
比占位图还小、色值还少,朴素判据会把它误判成"无数据"。
"""
import hashlib
import unittest
from pathlib import Path

from backend.config import EsriImageryConfig
from backend.providers.esri_imagery import (
    PLACEHOLDER_SHA256, build_esri_imagery_provider, is_esri_imagery_provider,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg(**kw) -> EsriImageryConfig:
    base = EsriImageryConfig(enabled=True, proxy="127.0.0.1:6789")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def _fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestRegistry(unittest.TestCase):
    def test_is_esri_imagery_provider(self):
        self.assertTrue(is_esri_imagery_provider("esri_imagery"))
        self.assertFalse(is_esri_imagery_provider("esri_terrain"))
        self.assertFalse(is_esri_imagery_provider("google_img"))

    def test_metadata(self):
        p = build_esri_imagery_provider(_cfg())
        self.assertEqual(p.key, "esri_imagery")
        self.assertEqual(p.ext, "jpg")
        self.assertEqual(p.bands, 3)


class TestTileUrl(unittest.TestCase):
    def test_arcgis_order_is_z_y_x(self):
        """ArcGIS 的 URL 顺序是 {z}/{y}/{x} —— 写成 {z}/{x}/{y} 会取到
        转置后的错误瓦片(图像看着像但地理位置错),是最隐蔽的一类 bug。"""
        p = build_esri_imagery_provider(_cfg())
        self.assertTrue(p.tile_url(53955, 24810, 16).endswith("/16/24810/53955"))

    def test_custom_template(self):
        p = build_esri_imagery_provider(
            _cfg(url_template="https://x.example.com/{z}/{y}/{x}.jpg"))
        self.assertEqual(p.tile_url(7, 8, 9), "https://x.example.com/9/8/7.jpg")

    def test_empty_template_rejected(self):
        with self.assertRaises(ValueError):
            build_esri_imagery_provider(_cfg(url_template=""))


class TestZoomAndProxy(unittest.TestCase):
    def test_max_zoom_is_19(self):
        """实测 z19 是亚欧城市上限且载有真实新增细节;z20 仅美国境内有。"""
        self.assertEqual(build_esri_imagery_provider(_cfg()).max_zoom(), 19)

    def test_max_zoom_configurable(self):
        self.assertEqual(
            build_esri_imagery_provider(_cfg(max_zoom=20)).max_zoom(), 20)

    def test_min_zoom_is_one(self):
        self.assertEqual(build_esri_imagery_provider(_cfg()).min_zoom(), 1)

    def test_proxy_normalized(self):
        p = build_esri_imagery_provider(_cfg(proxy="127.0.0.1:6789"))
        self.assertEqual(p.proxy, "http://127.0.0.1:6789")

    def test_no_missing_statuses(self):
        """Esri 用 200 + 占位图表达无数据,不用状态码。"""
        self.assertEqual(build_esri_imagery_provider(_cfg()).missing_statuses(),
                         frozenset())


class TestPlaceholderFingerprint(unittest.TestCase):
    def test_fixture_matches_recorded_sha(self):
        """夹具与代码里的指纹常量必须一致 —— 不一致说明有一方被改过。"""
        data = _fx("esri_wi_placeholder_2521B.jpg")
        self.assertEqual(hashlib.sha256(data).hexdigest(), PLACEHOLDER_SHA256)

    def test_fixture_size_is_2521(self):
        self.assertEqual(len(_fx("esri_wi_placeholder_2521B.jpg")), 2521)


class TestIsEmptyTile(unittest.TestCase):
    def setUp(self):
        self.p = build_esri_imagery_provider(_cfg())

    def test_placeholder_is_empty(self):
        """正样本:占位图必须识别。"""
        self.assertTrue(self.p.is_empty_tile(_fx("esri_wi_placeholder_2521B.jpg")))

    def test_real_ocean_tile_not_empty(self):
        """★ 关键负样本 ★

        678 字节的真实深海瓦片:比占位图更小(678 < 2521)、色值更少
        (11 < 79)。若 is_empty_tile 用"体量小 + 色值少"这类朴素规则,
        这条测试会红 —— 这正是它存在的意义。

        误判的后果是静默的数据空洞:深海区域在成果里变 nodata,
        且瓦片不入缓存,每次重跑都重新下载。
        """
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_ocean_678B.jpg")))

    def test_real_sahara_not_empty(self):
        """负样本:体量小(3578B)且明亮(mean 154.68)的真实瓦片。"""
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_sahara.jpg")))

    def test_real_beijing_not_empty(self):
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_beijing.jpg")))

    def test_real_tibet_not_empty(self):
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_tibet_z17.jpg")))

    def test_empty_bytes_not_empty_tile(self):
        """空响应不是"占位图",应交给上层按失败处理。"""
        self.assertFalse(self.p.is_empty_tile(b""))

    def test_garbage_not_empty_tile(self):
        """解码失败不应抛异常,也不该判成占位图。"""
        self.assertFalse(self.p.is_empty_tile(b"not an image at all"))

    def test_fallback_catches_similar_placeholder(self):
        """兜底启发式:构造一张体量接近、极亮、几乎无纹理的图,
        模拟 Esri 改版后的新占位图(sha 不再命中)。"""
        import io

        import numpy as np
        import rasterio

        arr = np.full((3, 256, 256), 205, dtype=np.uint8)
        arr[0, 0, 0] = 203      # 极小扰动,std 仍远低于 10
        buf = io.BytesIO()
        profile = {"driver": "JPEG", "height": 256, "width": 256,
                   "count": 3, "dtype": "uint8"}
        with rasterio.io.MemoryFile() as mem:
            with mem.open(**profile) as dst:
                dst.write(arr)
            data = mem.read()
        # 该构造图体量应落在 2000~3000 区间之外时跳过本断言(JPEG 压缩率不定)
        if 2000 <= len(data) <= 3000:
            self.assertTrue(self.p.is_empty_tile(data))


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_esri_imagery_provider -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.providers.esri_imagery'`

- [x] **Step 3: 创建 `backend/providers/esri_imagery.py`(provider 部分)**

```python
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
```

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_esri_imagery_provider -v`
Expected: PASS,20 项通过。**特别确认 `test_real_ocean_tile_not_empty` 是绿的** —— 它红了说明判据用了朴素规则。

- [x] **Step 5: 提交**

```bash
git add backend/providers/esri_imagery.py tests/test_esri_imagery_provider.py
git commit -m "feat(providers): 加 Esri World Imagery provider 与占位图两级判定

占位图跨大洲字节完全一致,故用精确 sha256 指纹为主判据(零误判),
保守启发式兜底(应对 Esri 改版)。

判据不能用"体量小+色值少":实测一张 678 字节的真实深海瓦片
(唯一色值 11)比占位图更小更单调,会被误判成无数据,造成静默
数据空洞且瓦片不入缓存。三个阈值必须同时满足。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: `formats.py` 网格登记

**Files:**
- Create: `tests/test_formats_grid.py`
- Modify: `backend/core/formats.py`

- [x] **Step 1: 写失败测试**

创建 `tests/test_formats_grid.py`:

```python
"""网格维度登记(geodetic / mercator)。

坐标系不是"数据语义"而是"网格约定",故不新开 DataKind —— 那要改 DataKind.ALL
及所有 (RASTER_IMAGE,) 元组,侵入面大。新增一张与 PROVIDER_KIND 并列的表。
"""
import unittest

from backend.core.formats import (
    GEO_GEODETIC, GEO_MERCATOR, DataKind, grid_of, kind_of, stages_for,
)


class TestGridOf(unittest.TestCase):
    def test_tianditu_is_geodetic(self):
        for k in ("tianditu_img", "tianditu_vec", "tianditu_ter"):
            self.assertEqual(grid_of(k), GEO_GEODETIC, k)

    def test_google_layers_are_mercator(self):
        for k in ("google_img", "google_hybrid", "google_road", "google_terrain"):
            self.assertEqual(grid_of(k), GEO_MERCATOR, k)

    def test_esri_imagery_is_mercator(self):
        self.assertEqual(grid_of("esri_imagery"), GEO_MERCATOR)

    def test_esri_terrain_is_mercator(self):
        """现有 DEM 本来就是墨卡托网格,登记后才能统一分流。"""
        self.assertEqual(grid_of("esri_terrain"), GEO_MERCATOR)

    def test_unknown_falls_back_to_geodetic(self):
        """未登记者回落 geodetic,与旧行为一致(旧代码无网格概念,全走 4326)。"""
        self.assertEqual(grid_of("nope_not_registered"), GEO_GEODETIC)
        self.assertEqual(grid_of(""), GEO_GEODETIC)


class TestKindOf(unittest.TestCase):
    def test_new_providers_are_raster_image(self):
        for k in ("google_img", "google_hybrid", "google_road",
                  "google_terrain", "esri_imagery"):
            self.assertEqual(kind_of(k), DataKind.RASTER_IMAGE, k)

    def test_dem_unchanged(self):
        self.assertEqual(kind_of("esri_terrain"), DataKind.RASTER_DEM)


class TestStagesUnchanged(unittest.TestCase):
    def test_new_providers_get_image_stages(self):
        """kind 是 RASTER_IMAGE,故自动获得 geotiff/tms/osm 三个阶段,
        不必为新数据源逐处加分支。"""
        keys = {s.key for s in stages_for(DataKind.RASTER_IMAGE)}
        self.assertIn("geotiff", keys)
        self.assertIn("tms", keys)
        self.assertIn("osm", keys)

    def test_not_routed_to_3d(self):
        """回归护栏:新 provider 绝不能被判成三维管线(那会走 runner_3d)。"""
        from backend.core.formats import is_3d_provider
        for k in ("google_img", "esri_imagery"):
            self.assertFalse(is_3d_provider(k), k)

    def test_not_local_source(self):
        from backend.core.formats import is_local_source
        for k in ("google_img", "esri_imagery"):
            self.assertFalse(is_local_source(k), k)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_formats_grid -v`
Expected: FAIL — `ImportError: cannot import name 'GEO_GEODETIC' from 'backend.core.formats'`

- [x] **Step 3: 改 `backend/core/formats.py`**

在 `PROVIDER_KIND` 字典**之前**插入网格表:

```python
# ---------- 数据源 → 网格类型 ----------
# 坐标系不是"数据语义"而是"网格约定",故不新开 DataKind —— 那要改 DataKind.ALL
# 及所有 (RASTER_IMAGE,) 元组,侵入面大。这里单独一张表,与 PROVIDER_KIND 并列。
# 术语沿用项目服务层已有的说法(core/service_scan.py、core/service_bounds.py)。

#: 网格类型
GEO_GEODETIC = "geodetic"   # EPSG:4326 经纬度瓦片(天地图 TileMatrixSet=c)
GEO_MERCATOR = "mercator"   # EPSG:3857 Web 墨卡托 XYZ

#: provider key -> 网格类型。未登记者回落 geodetic(与旧行为一致:
#: 旧代码没有网格概念,影像一律按 4326 处理)。
PROVIDER_GRID: dict[str, str] = {
    # 天地图:EPSG:4326 经纬度瓦片
    "tianditu_img": GEO_GEODETIC,
    "tianditu_vec": GEO_GEODETIC,
    "tianditu_ter": GEO_GEODETIC,
    # Google 影像:EPSG:3857 墨卡托 XYZ
    "google_img": GEO_MERCATOR,
    "google_hybrid": GEO_MERCATOR,
    "google_road": GEO_MERCATOR,
    "google_terrain": GEO_MERCATOR,
    # Esri World Imagery:同为墨卡托 XYZ,与 Google 网格完全同构
    # (实测 165 张瓦片 9 窗口互相关,偏移 ≤4m 且不随位置变化)
    "esri_imagery": GEO_MERCATOR,
    # 现有 DEM 本来就是墨卡托网格,登记后可统一分流
    "esri_terrain": GEO_MERCATOR,
    "aws_terrain": GEO_MERCATOR,
}


def grid_of(provider: str) -> str:
    """取数据源的网格类型。提交前即可调用,无需构造 provider 实例。"""
    if provider == "img":          # 兼容早期落库的短 key
        provider = "tianditu_img"
    return PROVIDER_GRID.get(provider, GEO_GEODETIC)
```

在 `PROVIDER_KIND` 字典内,`"tianditu_ter": DataKind.RASTER_IMAGE,` 那行之后插入:

```python
    # Google 影像 4 图层与 Esri World Imagery(EPSG:3857 墨卡托 XYZ)。
    # kind 与天地图同为 RASTER_IMAGE —— 这是有意的:网格差异由 PROVIDER_GRID
    # 表达,kind 只管"数据是什么"。因此新数据源自动获得 geotiff/tms/osm
    # 三个阶段,且 worker 的 _resolve_runner 会自然路由到 runner.run_task,
    # 无需改动进程隔离层。
    "google_img": DataKind.RASTER_IMAGE,
    "google_hybrid": DataKind.RASTER_IMAGE,
    "google_road": DataKind.RASTER_IMAGE,
    "google_terrain": DataKind.RASTER_IMAGE,
    "esri_imagery": DataKind.RASTER_IMAGE,
```

- [x] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_formats_grid -v`
Expected: PASS,11 项通过

- [x] **Step 5: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿

- [x] **Step 6: 提交**

```bash
git add backend/core/formats.py tests/test_formats_grid.py
git commit -m "feat(formats): 引入网格维度并登记 5 个新影像数据源

新增 PROVIDER_GRID 表(geodetic/mercator),与 PROVIDER_KIND 并列。
不新开 DataKind:坐标系是网格约定而非数据语义,新开会牵动 DataKind.ALL
及所有 (RASTER_IMAGE,) 元组。

新 provider 的 kind 仍是 RASTER_IMAGE,因此自动获得 geotiff/tms/osm
阶段,且 worker 会自然路由到 runner.run_task,进程隔离层零改动。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: runner 按网格分流(provider 构造 + 瓦片区间 + 拼接)

`core/runner.py` 现在按 `is_dem` 二选一决定用哪套瓦片数学。要改成按**网格**分流,让影像也能走 3857。

> 本任务**不碰** `core/worker.py` 与 `core/scheduler.py`。新 provider 的 kind 是 `RASTER_IMAGE`,`_resolve_runner` 会自然路由到 `runner.run_task`。若发现"必须改 worker.py",说明抽象放错了位置,停下来重看设计 §4.6。

**Files:**
- Create: `tests/test_runner_grid.py`
- Modify: `backend/core/runner.py`

- [x] **Step 1: 写失败测试**

创建 `tests/test_runner_grid.py`:

```python
"""runner 的网格分流:provider 构造、瓦片区间函数、拼接坐标系。

不跑真实下载,只验证"按 provider 选对了哪套函数/参数"。
"""
import unittest

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import _crs_for_grid, _range_fn_for, _build_provider_for


class _Task(dict):
    pass


class TestRangeFnFor(unittest.TestCase):
    def test_geodetic_uses_4326_range(self):
        from backend.core.tiling import range_for_bbox
        self.assertIs(_range_fn_for("tianditu_img"), range_for_bbox)

    def test_mercator_uses_xyz_range(self):
        from backend.core.mercator_tiling import mercator_range_for_bbox
        for k in ("google_img", "esri_imagery", "esri_terrain"):
            self.assertIs(_range_fn_for(k), mercator_range_for_bbox, k)

    def test_unknown_falls_back_to_geodetic(self):
        from backend.core.tiling import range_for_bbox
        self.assertIs(_range_fn_for("unknown_provider"), range_for_bbox)


class TestCrsForGrid(unittest.TestCase):
    def test_geodetic_is_4326(self):
        self.assertEqual(_crs_for_grid(GEO_GEODETIC), "EPSG:4326")

    def test_mercator_is_3857(self):
        self.assertEqual(_crs_for_grid(GEO_MERCATOR), "EPSG:3857")


class TestBuildProviderFor(unittest.TestCase):
    """provider 构造分支:每种 provider 走对应的构造器。"""

    def test_google(self):
        from backend.providers.google import GoogleProvider
        p = _build_provider_for(_Task(provider="google_img"))
        self.assertIsInstance(p, GoogleProvider)
        self.assertEqual(p.key, "google_img")

    def test_esri_imagery(self):
        from backend.providers.esri_imagery import EsriImageryProvider
        p = _build_provider_for(_Task(provider="esri_imagery"))
        self.assertIsInstance(p, EsriImageryProvider)

    def test_dem_unchanged(self):
        from backend.providers.terrain import TerrainProvider
        p = _build_provider_for(_Task(provider="esri_terrain"))
        self.assertIsInstance(p, TerrainProvider)

    def test_tianditu_unchanged(self):
        from backend.providers.tianditu import TiandituProvider
        p = _build_provider_for(_Task(provider="tianditu_img"))
        self.assertIsInstance(p, TiandituProvider)

    def test_google_road_bands_is_one(self):
        """端到端确认:构造出来的 provider 波段数正确(拼接靠它)。"""
        p = _build_provider_for(_Task(provider="google_road"))
        self.assertEqual(p.bands, 1)


class TestNoWorkerChanges(unittest.TestCase):
    """回归护栏:新 provider 必须走 2D runner,不能被路由到 runner_3d。"""

    def test_resolve_runner_routes_to_2d(self):
        from backend.core.formats import is_3d_provider
        for k in ("google_img", "google_road", "esri_imagery"):
            self.assertFalse(is_3d_provider(k), k)


if __name__ == "__main__":
    unittest.main()
```

- [x] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_runner_grid -v`
Expected: FAIL — `ImportError: cannot import name '_crs_for_grid' from 'backend.core.runner'`

- [x] **Step 3: 在 `backend/core/runner.py` 加三个辅助函数**

先在 import 段补充(在 `from .formats import ...` 那行里加 `GEO_MERCATOR, grid_of`):

```python
from .formats import (CONTAINERS, GEO_MERCATOR, DataKind, STAGES, grid_of,
                      is_local_source, kind_of, resolve_outputs)
```

并加两个 provider 的导入(放在现有 `from ..providers.terrain import ...` 附近):

```python
from ..providers.esri_imagery import (build_esri_imagery_provider,
                                      is_esri_imagery_provider)
from ..providers.google import build_google_provider, is_google_provider
```

然后在 `run_task` 函数**之前**(`_safe_unlink` 之后)插入三个辅助函数:

```python
def _range_fn_for(provider: str):
    """按数据源的网格返回瓦片区间函数。

    geodetic(天地图)用 4326 的 range_for_bbox;mercator(Google/Esri 影像、
    Esri DEM)用墨卡托 XYZ 的 mercator_range_for_bbox。两者不能混用 ——
    混了会按错误的网格取瓦片,下出来的图整体错位。
    """
    if grid_of(provider) == GEO_MERCATOR:
        return mercator_range_for_bbox
    return range_for_bbox


def _crs_for_grid(grid: str) -> str:
    """网格对应的拼接坐标系。"""
    return "EPSG:3857" if grid == GEO_MERCATOR else "EPSG:4326"


def _build_provider_for(task):
    """按 provider key 构造数据源实例(分发即守卫)。

    分支顺序无所谓(各 is_xxx 互斥),但必须都在 —— 漏一个会静默回落到
    build_provider 并报"暂不支持的数据源"。
    """
    key = task["provider"]
    if is_dem_provider(key):
        return build_terrain_provider(key)
    if is_google_provider(key):
        return build_google_provider(key, settings.google)
    if is_esri_imagery_provider(key):
        return build_esri_imagery_provider(settings.esri_imagery)
    token_src = (token_pool.use_token if token_pool.has_any()
                 else settings.tianditu.token)
    return build_provider(key, token_src)
```

- [x] **Step 4: 改 `run_task` 里的 provider 构造**

把现有的这段(约 110-118 行):

```python
    if local_src is not None:
        provider = LocalFileProvider(task["provider"], local_src)
    elif is_dem:
        provider = build_terrain_provider(task["provider"])
    else:
        token_src = token_pool.use_token if token_pool.has_any() else settings.tianditu.token
        provider = build_provider(task["provider"], token_src)
```

改为:

```python
    if local_src is not None:
        provider = LocalFileProvider(task["provider"], local_src)
    else:
        provider = _build_provider_for(task)
```

- [x] **Step 5: 改注记的条件**

注记只有天地图有。现有条件是 `not is_dem`,对新 provider 会误判为"可以有注记"。把(约 123 行):

```python
    annotate = (task.get("annotate", False) and not is_dem
                and local_src is None)
```

改为:

```python
    # 注记是天地图特有的同网格覆盖层(cia/cva/cta)。DEM、本地文件源、
    # Google/Esri 都没有这个概念 —— 用 grid 判定而非逐个列 provider:
    # 墨卡托源一律无注记,新增墨卡托数据源时不必再回来改这里。
    annotate = (task.get("annotate", False) and not is_dem
                and local_src is None
                and grid_of(task["provider"]) != GEO_MERCATOR)
```

- [x] **Step 6: 改下载阶段的区间函数**

把(约 190 行)`range_fn = mercator_range_for_bbox if is_dem else range_for_bbox` 改为:

```python
        # 按网格取区间函数:影像也可能是墨卡托(Google/Esri),不能再按 is_dem 二选一
        range_fn = _range_fn_for(task["provider"])
```

- [x] **Step 7: 在 `_ExportCtx` 里带上网格**

找到 `_ExportCtx` 的 dataclass 定义,加一个字段:

```python
    #: 数据源网格("geodetic" / "mercator")。各导出阶段据此选瓦片数学与拼接 crs。
    grid: str = "geodetic"
```

在 `run_task` 里构造 `ctx` 时传入(在 `is_dem=is_dem,` 之后加):

```python
        grid=grid_of(task["provider"]),
```

- [x] **Step 8: 改 `_stage_geotiff` 按网格拼接**

把 `_stage_geotiff` 里的(约 470 行):

```python
    trs = {z: range_for_bbox(*ctx.bbox, z) for z in levels}
```

改为:

```python
    range_fn = _range_fn_for(task["provider"])
    trs = {z: range_fn(*ctx.bbox, z) for z in levels}
    mosaic_crs = _crs_for_grid(ctx.grid)
```

把同函数内的 `mosaic_to_geotiff(...)` 调用改为带 crs:

```python
            mosaic_to_geotiff(ctx.provider, settings.cache_dir, tr, geotiff,
                              ctx.downloader.tile_path, anno_path_fn,
                              on_row=on_row, crs=mosaic_crs)
```

同时把该函数内关于主文件坐标系的注释更新为:

```python
        # 主文件的坐标系跟随数据源网格:天地图出 4326,Google/Esri 出 3857。
        # 下游 tms/osm 会据此决定是否需要重投影(见 Task 18/19)。
        # 裁剪在主文件坐标系下做;几何是 WGS84,3857 主文件需先转换(见 _clip_geom_of)。
```

- [x] **Step 9: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_runner_grid -v`
Expected: PASS,11 项通过

- [x] **Step 10: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿。**特别确认 `test_scheduler` 与 `test_worker_dispatch` 是绿的** —— 它们红了说明改动意外侵入了进程隔离层,停下来看为什么,不要改测试。

- [x] **Step 11: 提交**

```bash
git add backend/core/runner.py tests/test_runner_grid.py
git commit -m "feat(runner): 按网格分流瓦片区间与拼接坐标系

原先按 is_dem 二选一决定用哪套瓦片数学,影像被绑死在 4326。改为按
PROVIDER_GRID 分流,墨卡托影像(Google/Esri)走 XYZ 区间 + 3857 拼接。

注记条件也改为按 grid 判定:墨卡托源一律无注记,新增同类数据源时
不必再回来改这里。

scheduler/worker 零改动:新 provider 的 kind 是 RASTER_IMAGE,
_resolve_runner 自然路由到 runner.run_task。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: 阶段二端到端验收(真实下载 + 空瓦片行为)

这是**必须过的门**。若此处不过,后面所有导出格式都在错误的成果上做验证。

**Files:**
- Create: `docs/验证记录-google-esri影像.md`

- [x] **Step 1: 开启配置**

编辑 `config.yaml`(不是 example),把两个源打开并填代理:

```yaml
google:
  enabled: true
  proxy: "127.0.0.1:6789"

esri_imagery:
  enabled: true
  proxy: "127.0.0.1:6789"
```

- [x] **Step 2: 启动服务并提交 Google 小范围任务**

Run: `start.bat`(另开终端),浏览器打开 http://127.0.0.1:8000

提交任务:数据源 `google_img`,范围画在北京北四环一带(约 116.380~116.392 / 39.995~40.005,0.01°),级别 z15~z17,导出勾选「合并 GeoTIFF」。

Expected:任务正常完成,`downloaded` 与预估瓦片数一致,`failed` 为 0。

- [x] **Step 3: 验证产出的坐标系与坐标值**

Run:
```bash
.venv/Scripts/python.exe -c "
import rasterio, glob
for f in sorted(glob.glob('output/*/*_z17.tif')):
    with rasterio.open(f) as ds:
        print(f)
        print('  crs =', ds.crs)
        print('  bounds =', ds.bounds)
        print('  shape =', ds.shape, 'bands =', ds.count)
"
```
Expected:`crs = EPSG:3857`,bounds 是米制大数(约 x≈1.295e7、y≈4.86e6),不是经纬度小数。

- [x] **Step 4: QGIS 对齐抽查**

在 QGIS 打开该 GeoTIFF,叠加一个在线底图(如 OSM XYZ),确认建筑/道路对齐,抽查偏移 ≤4m。

> 若整体错位到非洲/南极附近,说明坐标系与坐标值不匹配 —— 回看 Task 2 的 bounds 分支与 Task 9 的 `mosaic_crs` 传参。

- [x] **Step 5: 验证 `google_road` 的单波段路径**

提交同范围、数据源 `google_road`、级别 z16 的任务。

Expected:任务完成且**不报 `DatasetIOShapeError`**。

Run:
```bash
.venv/Scripts/python.exe -c "
import rasterio, glob
for f in sorted(glob.glob('output/*road*/*_z16.tif')):
    with rasterio.open(f) as ds:
        print(f, 'bands =', ds.count, 'crs =', ds.crs)
"
```
Expected:能正常打开(波段数为 1 或经调色板展开后为 3,取决于 `_read_tile` 的展开行为),crs 为 EPSG:3857。

- [x] **Step 6: 【关键】验证占位图不污染缓存(设计清单第 13 条)**

提交任务:数据源 `esri_imagery`,范围选**南海海域**(114.0~114.1 / 15.0~15.1),级别 z16。

Expected:任务**完成而非失败**(该处确实无数据),`failed` 为 0。

Run:
```bash
.venv/Scripts/python.exe -c "
from pathlib import Path
import hashlib
PH = '9eafd300d61393184a4abc1d458564cfd1cd9b6f9c4e9c74687045c0a0e5b858'
root = Path('data/tiles/esri_imagery')
n_ph = n_all = 0
for p in root.rglob('*.jpg'):
    n_all += 1
    if hashlib.sha256(p.read_bytes()).hexdigest() == PH:
        n_ph += 1
        print('占位图被写入缓存:', p)
print(f'缓存瓦片总数={n_all} 其中占位图={n_ph}')
assert n_ph == 0, '占位图污染了缓存,is_empty_tile 未生效'
print('通过:缓存无占位图')
"
```
Expected:`其中占位图=0`,打印「通过」。

- [x] **Step 7: 【关键】验证真实深海瓦片不被误判(设计清单第 14 条)**

提交任务:数据源 `esri_imagery`,范围选**太平洋**(-140.0~-139.9 / -20.0~-19.9),级别 z16。

Expected:该处 Esri 有真实(暗色)影像,瓦片应**正常写入缓存**。

Run:
```bash
.venv/Scripts/python.exe -c "
from pathlib import Path
tiles = list(Path('data/tiles/esri_imagery/16').glob('*.jpg'))
small = [p for p in tiles if p.stat().st_size < 1500]
print(f'z16 缓存瓦片 {len(tiles)} 张,其中 <1.5KB 的 {len(small)} 张')
assert tiles, '一张都没缓存,真实瓦片被误判成占位图了'
print('通过:真实深海瓦片已缓存')
"
```
Expected:有瓦片被缓存,打印「通过」。

> 这两条(Step 6 与 7)必须**都**过。只过 Step 6 说明判据过于激进(把真实数据也当空的);只过 Step 7 说明判据没生效。

- [x] **Step 8: 验证 Google 404 分流(设计清单第 16 条)**

提交任务:数据源 `google_img`,范围选**渤海近岸**(119.5~119.55 / 38.5~38.55),级别 z16。

Expected:任务**不报"端点可能已变更"**,正常完成;成果对应区域为 nodata。

查看日志确认 404 瓦片只请求了 1 次(而非 4 次):

Run: `grep -c "404" logs/*.log`(或在界面的运行日志里查看)

- [x] **Step 9: 写验证记录**

创建 `docs/验证记录-google-esri影像.md`,记录:

```markdown
# 验证记录 — Google / Esri 影像数据源

> 日期:(填写实际日期)
> 环境:Windows,本机代理 127.0.0.1:6789

## 阶段二:下载与拼接

| 项 | 数据源 | 范围 | 级别 | 结果 |
|---|---|---|---|---|
| 基本下载 | google_img | 北京 0.01° | z15-17 | (填 瓦片数/失败数/耗时) |
| 坐标系 | google_img | 同上 | z17 | crs=(填) bounds=(填) |
| QGIS 对齐 | google_img | 同上 | z17 | 偏移(填)m |
| 单波段 | google_road | 同上 | z16 | (填 是否报错) |
| 占位图不入缓存 | esri_imagery | 南海 | z16 | 占位图数=(填) |
| 真实深海不误判 | esri_imagery | 太平洋 | z16 | 已缓存=(填)张 |
| 404 分流 | google_img | 渤海 | z16 | 是否误报端点失效=(填) |

## 结论

(填写:通过 / 发现的问题及处理)
```

- [x] **Step 10: 提交**

```bash
git add docs/验证记录-google-esri影像.md
git commit -m "docs: 补阶段二(下载与拼接)验收记录

含两条关键验收:占位图不污染缓存、真实深海瓦片不被误判。
两条必须都过 —— 只过前者说明判据过于激进,只过后者说明判据未生效。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

# 阶段三:提交路径安全与规模可见

## Task 11: `suggest_mercator_levels` 建议级别

现状 `api/tasks.py:130` 是"DEM 走 `suggest_dem_levels`,其余走 `suggest_levels`"。墨卡托**影像**两个都不能用:`suggest_levels` 是 4326 网格数学;`suggest_dem_levels` 的级别下限是 0、预算是 2000(为 LERC 解码慢而定)。

**Files:**
- Create: `tests/test_mercator_suggest.py`
- Modify: `backend/core/mercator_tiling.py`
- Modify: `backend/core/dem_tiling.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_mercator_suggest.py`:

```python
"""墨卡托影像的建议级别,以及 suggest_dem_levels 不回归。

两者共用判据实现但参数不同:
  DEM  : z_floor=0, tile_budget=2000(LERC 解码慢)
  影像 : z_floor=1, tile_budget=8000(jpg 解码快;z0 对影像无意义)
"""
import unittest

from backend.core.mercator_tiling import suggest_mercator_levels

BBOX_SMALL = (116.36, 39.98, 116.41, 40.03)      # 0.05 度,约一个城区
BBOX_BIG = (116.0, 39.5, 117.0, 40.5)            # 1 度


class TestSuggestMercatorLevels(unittest.TestCase):
    def test_returns_expected_keys(self):
        got = suggest_mercator_levels(BBOX_SMALL, 21)
        for k in ("levels", "recommended", "recommended_tiles",
                  "budget_limited", "max_useful", "min_useful"):
            self.assertIn(k, got, k)

    def test_z_floor_is_one_not_zero(self):
        """影像的 z0 单张盖全球,毫无意义。天地图影像的下限就是 1。"""
        zs = [r["z"] for r in suggest_mercator_levels(BBOX_SMALL, 21)["levels"]]
        self.assertEqual(min(zs), 1)
        self.assertNotIn(0, zs)

    def test_recommended_excludes_zero(self):
        rec = suggest_mercator_levels(BBOX_SMALL, 21)["recommended"]
        self.assertNotIn(0, rec)

    def test_respects_max_cap(self):
        zs = [r["z"] for r in suggest_mercator_levels(BBOX_SMALL, 19)["levels"]]
        self.assertEqual(max(zs), 19)

    def test_recommended_within_budget(self):
        got = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=8000)
        self.assertLessEqual(got["recommended_tiles"], 8000)

    def test_recommended_not_topping_out_at_21(self):
        """默认预算下不该推荐到 z21(0.05 度选区 z21 是 11 万张)。"""
        rec = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=8000)["recommended"]
        self.assertTrue(rec)
        self.assertLess(max(rec), 21)

    def test_big_bbox_is_budget_limited(self):
        got = suggest_mercator_levels(BBOX_BIG, 21, tile_budget=8000)
        self.assertTrue(got["budget_limited"])

    def test_custom_budget_changes_recommendation(self):
        lo = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=500)
        hi = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=50000)
        self.assertLessEqual(max(lo["recommended"]), max(hi["recommended"]))


class TestDemSuggestNotRegressed(unittest.TestCase):
    """回归护栏:提取共用实现后,DEM 版的行为必须逐字段不变。"""

    def test_dem_still_starts_at_zero(self):
        from backend.core.dem_tiling import suggest_dem_levels
        zs = [r["z"] for r in suggest_dem_levels(BBOX_SMALL, 16)["levels"]]
        self.assertEqual(min(zs), 0)

    def test_dem_default_cap_is_16(self):
        from backend.core.dem_tiling import suggest_dem_levels
        zs = [r["z"] for r in suggest_dem_levels(BBOX_SMALL)["levels"]]
        self.assertEqual(max(zs), 16)

    def test_dem_keys_unchanged(self):
        from backend.core.dem_tiling import suggest_dem_levels
        got = suggest_dem_levels(BBOX_SMALL, 16)
        for k in ("levels", "recommended", "recommended_tiles",
                  "budget_limited", "max_useful", "min_useful"):
            self.assertIn(k, got, k)

    def test_dem_level_row_shape_unchanged(self):
        from backend.core.dem_tiling import suggest_dem_levels
        row = suggest_dem_levels(BBOX_SMALL, 16)["levels"][0]
        self.assertEqual(set(row), {"z", "tiles", "ratio", "useful"})


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_mercator_suggest -v`
Expected: FAIL — `ImportError: cannot import name 'suggest_mercator_levels'`

- [ ] **Step 3: 在 `mercator_tiling.py` 加共用实现与影像版**

在 `backend/core/mercator_tiling.py` 末尾追加:

```python
def suggest_levels_mercator(
    bbox: tuple[float, float, float, float],
    z_max_cap: int,
    z_floor: int,
    min_useful_ratio: float,
    tile_budget: int,
    depth: int,
) -> dict:
    """墨卡托网格的建议级别判据(DEM 与影像共用的内核)。

    判据同 tiling.suggest_levels:瓦片是固定网格,低级别单张就能盖住远超选区
    的范围(实测 0.07° 选区在天地图第 7 级只有 0.1% 有效占比),全选会下一堆
    几乎全是选区外内容的图。这里在 3857 下算面积占比。

    调用方传各自的参数,不要在这里塞默认值 —— DEM 与影像的取舍不同
    (见两个包装函数的说明),混在一起会让两套取舍互相污染。
    """
    from rasterio.warp import transform_bounds

    w, s, e, n = bbox
    # 选区面积在 3857 下算,与瓦片覆盖面积同坐标系才可比
    sw, ss, se, sn = transform_bounds("EPSG:4326", "EPSG:3857", w, s, e, n)
    sel_area = max((se - sw) * (sn - ss), 1e-9)

    rows = []
    for z in range(z_floor, z_max_cap + 1):
        tr = mercator_range_for_bbox(w, s, e, n, z)
        bw, bs, be, bn = mosaic_bounds_3857(tr)
        cov = max((be - bw) * (bn - bs), 1e-9)
        ratio = min(sel_area / cov, 1.0)
        rows.append({"z": z, "tiles": tr.count, "ratio": round(ratio, 4),
                     "useful": ratio >= min_useful_ratio})

    tiles_of = {r["z"]: r["tiles"] for r in rows}
    useful = [r["z"] for r in rows if r["useful"]]
    pool = useful or [r["z"] for r in rows]

    top = pool[0]
    for z in pool:
        if tiles_of[z] <= tile_budget:
            top = z
        else:
            break
    budget_limited = top < pool[-1]

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


def suggest_mercator_levels(
    bbox: tuple[float, float, float, float],
    z_max_cap: int = 21,
    min_useful_ratio: float = 0.25,
    tile_budget: int = 8000,
    depth: int = 3,
) -> dict:
    """墨卡托**影像**的建议级别(Google / Esri World Imagery)。

    与 DEM 版(suggest_dem_levels)的两处差别,都是实测后定的:

      - **级别从 1 起而非 0**:z0 单张瓦片盖全球,对影像毫无意义。
        天地图影像的下限也是 1(api/tasks.py 的 z_floor)。
      - **预算 8000 而非 2000**:2000 是为 LERC 瓦片解码慢而定的
        (见 suggest_dem_levels 的说明);影像瓦片是 jpg,解码快得多,
        沿用 2000 会把推荐级别压得过低。

    ⚠️ tile_budget=8000 是推断值,尚未按实际拼接耗时校准
    (设计 §9 Q2)。一期实测后回写设计文档与此处默认值。

    这一道是取消瓦片数硬上限后最重要的护栏:默认值合理地低,用户
    就不会无意间触发 TB 级任务(设计 §4.10)。
    """
    return suggest_levels_mercator(bbox, z_max_cap, 1, min_useful_ratio,
                                   tile_budget, depth)
```

- [ ] **Step 4: 把 `suggest_dem_levels` 改为调用共用内核**

在 `backend/core/dem_tiling.py` 里,把 `suggest_dem_levels` 的函数体(从 `from rasterio.warp import transform_bounds` 到 `return {...}` 结束)**整体替换**为一行调用,保留原 docstring:

```python
def suggest_dem_levels(
    bbox: tuple[float, float, float, float],
    z_max_cap: int = 16,
    min_useful_ratio: float = 0.25,
    tile_budget: int = 2000,
    depth: int = 3,
) -> dict:
    """DEM 版的建议级别(判据同 tiling.suggest_levels,换成墨卡托网格算面积)。

    与影像的差别:
      - 网格是 EPSG:3857 墨卡托,列行数与 4326 不同,故面积在 3857 下算
      - 预算更小(2000 张):LERC 瓦片解码 + 拼接比影像慢,且高程成果通常不需要
        叠很多级——一张够精度的高程图比一套金字塔更常用
      - 级别从 0 起(Esri Terrain3D 允许 0 级)

    判据内核已抽到 mercator_tiling.suggest_levels_mercator(影像版共用),
    本函数只负责传 DEM 的参数。行为与抽取前逐字段一致
    (回归护栏见 tests/test_mercator_suggest.py)。
    """
    from .mercator_tiling import suggest_levels_mercator
    return suggest_levels_mercator(bbox, z_max_cap, 0, min_useful_ratio,
                                   tile_budget, depth)
```

- [ ] **Step 5: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_mercator_suggest -v`
Expected: PASS,12 项通过

- [ ] **Step 6: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿(DEM 建议级别的现有用例应不受影响)

- [ ] **Step 7: 提交**

```bash
git add backend/core/mercator_tiling.py backend/core/dem_tiling.py tests/test_mercator_suggest.py
git commit -m "feat(tiling): 加墨卡托影像的建议级别,判据内核与 DEM 版共用

影像版 z_floor=1(z0 盖全球对影像无意义)、tile_budget=8000
(DEM 的 2000 是为 LERC 解码慢定的,jpg 沿用会把推荐级别压得过低)。

判据内核抽为 suggest_levels_mercator,两版各传自己的参数 ——
不共用同一个函数名,以免两套取舍互相污染。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 12: Esri 区域最高级别探测 `probe_max_level`

实测:西藏/青海/新疆无人区在 **z18 就是整片占位图**(最高 z17),城市可到 z19。用户按全局常量选级别,在西部会下到一整片灰色。

**Files:**
- Modify: `backend/providers/esri_imagery.py`
- Modify: `tests/test_esri_imagery_provider.py`

- [ ] **Step 1: 追加失败测试**

在 `tests/test_esri_imagery_provider.py` 末尾(`if __name__` 之前)追加:

```python
class TestProbeMaxLevel(unittest.TestCase):
    """区域最高级别探测。

    不打真实网络:注入一个假的取瓦片函数,模拟"某级别以上返回占位图"。
    """

    def _probe(self, real_max: int | None, cfg=None, **kw):
        """real_max=None 模拟网络全失败。"""
        from backend.providers import esri_imagery as mod

        placeholder = _fx("esri_wi_placeholder_2521B.jpg")
        real = _fx("esri_wi_real_beijing.jpg")
        calls = []

        def fake_fetch(provider, col, row, z, timeout=10):
            calls.append(z)
            if real_max is None:
                return None                      # 取不到 = 判不了
            return real if z <= real_max else placeholder

        orig = mod._fetch_tile_bytes
        mod._fetch_tile_bytes = fake_fetch
        try:
            got = mod.probe_max_level((116.38, 39.99, 116.39, 40.00),
                                      cfg=cfg or _cfg(), **kw)
        finally:
            mod._fetch_tile_bytes = orig
        return got, calls

    def test_finds_city_level_19(self):
        got, _calls = self._probe(19)
        self.assertEqual(got, 19)

    def test_finds_remote_level_17(self):
        """西部无人区:z18 起是占位图,应返回 17。"""
        got, _calls = self._probe(17)
        self.assertEqual(got, 17)

    def test_probes_downward_from_cap(self):
        """从服务级天花板往下探,命中即停 —— 不该把所有级别都探一遍。"""
        _got, calls = self._probe(17)
        self.assertEqual(calls[0], 19)           # 从 max_zoom 开始
        self.assertEqual(calls, [19, 18, 17])    # 命中 17 即停

    def test_network_failure_returns_none(self):
        """None = 判不了。调用方据此放行用户选的级别,
        而不是误判成"该范围没有影像"把级别降到最低。"""
        got, _calls = self._probe(None)
        self.assertIsNone(got)

    def test_all_placeholder_returns_min_zoom(self):
        """逐级都是占位图(如公海):确实无影像,返回最低级别兜底。"""
        got, _calls = self._probe(0)
        self.assertEqual(got, 1)                 # min_zoom

    def test_respects_configured_cap(self):
        got, calls = self._probe(19, cfg=_cfg(max_zoom=18))
        self.assertEqual(calls[0], 18)
        self.assertEqual(got, 18)
```

另外在该文件顶部的 import 段补充(用于缓存测试):

```python
import time
```

再追加缓存测试:

```python
class TestProbeCache(unittest.TestCase):
    def setUp(self):
        from backend.providers import esri_imagery as mod
        mod._probe_cache.clear()

    def test_same_bbox_hits_cache(self):
        """探测是逐级网络请求,用户拖拽选区会连续触发,缓存是必需不是优化。"""
        from backend.providers import esri_imagery as mod

        real = _fx("esri_wi_real_beijing.jpg")
        calls = []

        def fake_fetch(provider, col, row, z, timeout=10):
            calls.append(z)
            return real

        orig = mod._fetch_tile_bytes
        mod._fetch_tile_bytes = fake_fetch
        try:
            bbox = (116.38, 39.99, 116.39, 40.00)
            a = mod.probe_max_level(bbox, cfg=_cfg())
            n_after_first = len(calls)
            b = mod.probe_max_level(bbox, cfg=_cfg())
        finally:
            mod._fetch_tile_bytes = orig
        self.assertEqual(a, b)
        self.assertEqual(len(calls), n_after_first)   # 第二次没有新请求

    def test_failure_not_cached(self):
        """判不了(None)不该缓存 —— 否则代理刚起来时的一次失败会粘住 24 小时。"""
        from backend.providers import esri_imagery as mod

        calls = []

        def fake_fail(provider, col, row, z, timeout=10):
            calls.append(z)
            return None

        orig = mod._fetch_tile_bytes
        mod._fetch_tile_bytes = fake_fail
        try:
            bbox = (116.38, 39.99, 116.39, 40.00)
            mod.probe_max_level(bbox, cfg=_cfg())
            n1 = len(calls)
            mod.probe_max_level(bbox, cfg=_cfg())
        finally:
            mod._fetch_tile_bytes = orig
        self.assertGreater(len(calls), n1)           # 第二次重新探测

    def test_cache_disabled_when_hours_zero(self):
        from backend.providers import esri_imagery as mod

        real = _fx("esri_wi_real_beijing.jpg")
        calls = []

        def fake_fetch(provider, col, row, z, timeout=10):
            calls.append(z)
            return real

        orig = mod._fetch_tile_bytes
        mod._fetch_tile_bytes = fake_fetch
        try:
            bbox = (116.38, 39.99, 116.39, 40.00)
            cfg = _cfg(probe_cache_hours=0)
            mod.probe_max_level(bbox, cfg=cfg)
            n1 = len(calls)
            mod.probe_max_level(bbox, cfg=cfg)
        finally:
            mod._fetch_tile_bytes = orig
        self.assertGreater(len(calls), n1)
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_esri_imagery_provider -v`
Expected: FAIL — `AttributeError: module 'backend.providers.esri_imagery' has no attribute '_fetch_tile_bytes'`

- [ ] **Step 3: 在 `esri_imagery.py` 追加探测实现**

在文件末尾追加:

```python
# ---------- 区域最高级别探测 ----------
#
# 为什么必须有:实测 Esri 的最高可用级别**随地理位置变化** ——
#   上海/广州/成都/纽约/伦敦/东京/悉尼:z19 有数据
#   北京/拉萨/乌鲁木齐/喀什/漠河        :z18(z19 是占位图)
#   格尔木/可可西里/塔克拉玛干/阿里      :z17(z18 已是占位图)
# 用户按全局 max_zoom 选级别,在西部会下到一整片灰色占位图。
#
# 这与项目现有 DEM 的行为完全同构(providers/terrain.py::probe_max_level
# 的注释原文:"最高 LOD 随地理位置变化,新疆等西部区域实测只到 14 级"),
# 故沿用同一模式而非重新设计。
#
# ⚠️ 与 terrain 版的关键差别:**不能用 urllib 直连**。
# Terrain3D 直连可用,故它用 urllib 是合理的;World Imagery 直连全部超时,
# 照抄会让探测永远失败并静默降级到最低级别。必须走 provider 的 proxy。

#: 探测结果缓存:量化后的 bbox key -> (最高级别, 写入时间戳)
_probe_cache: dict[tuple, tuple[int, float]] = {}

#: bbox 量化粒度(度)。用户拖拽选区时相邻请求会落到同一格,复用结果。
_PROBE_GRID_DEG = 0.1


def _probe_cache_key(bbox, cap: int) -> tuple:
    w, s, e, n = bbox
    q = _PROBE_GRID_DEG
    return (round(w / q), round(s / q), round(e / q), round(n / q), cap)


def _fetch_tile_bytes(provider: "EsriImageryProvider", col: int, row: int,
                      z: int, timeout: int = 10) -> bytes | None:
    """同步取一张瓦片;失败返回 None。

    用 requests 风格的同步调用而非 aiohttp:本函数由 api 层放进
    asyncio.to_thread 调用(与现有 _probe_dem_max_level 一致),
    在线程里再起事件循环会把简单的事复杂化。

    必须带代理 —— 见本节顶部的警告。
    """
    import urllib.error
    import urllib.request

    url = provider.tile_url(col, row, z)
    proxy = provider.proxy
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=provider.headers)
        with opener.open(req, timeout=timeout) as resp:
            if resp.status != 200:
                return None
            return resp.read()
    except urllib.error.HTTPError:
        # 明确的 HTTP 错误:当作"该级别没有"(与 terrain 版一致)
        return b""
    except Exception:
        # 网络/代理问题:判不了
        return None


def probe_max_level(bbox: tuple[float, float, float, float],
                    cfg=None, timeout: int = 10) -> int | None:
    """探测 Esri World Imagery 在某范围的最高可用级别。

    从服务级天花板(cfg.max_zoom)往下,取范围中心瓦片逐级探测,
    返回第一个有真实影像的级别。

    返回 None 表示**无法判定**(网络不通/代理未配)。调用方据此放行用户选的
    级别,而不是误判成"该范围没有影像"把级别降到最低 —— 与 terrain 版的
    返回值语义一致(providers/terrain.py:129 的注释)。

    逐级都是占位图(如公海)则返回 min_zoom 兜底:该范围确实无影像。
    """
    from ..config import settings
    from ..core.mercator_tiling import mercator_range_for_bbox

    cfg = cfg if cfg is not None else settings.esri_imagery
    provider = EsriImageryProvider(cfg)
    zmax, zmin = provider.max_zoom(), provider.min_zoom()

    cache_hours = float(getattr(cfg, "probe_cache_hours", 0) or 0)
    ckey = _probe_cache_key(bbox, zmax)
    if cache_hours > 0:
        hit = _probe_cache.get(ckey)
        if hit is not None:
            level, ts = hit
            import time as _time
            if _time.time() - ts < cache_hours * 3600:
                return level

    determined = False
    found: int | None = None
    for z in range(zmax, zmin - 1, -1):
        tr = mercator_range_for_bbox(*bbox, z)
        cx = (tr.col_min + tr.col_max) // 2
        cy = (tr.row_min + tr.row_max) // 2
        data = _fetch_tile_bytes(provider, cx, cy, z, timeout=timeout)
        if data is None:
            # 判不了这一级;继续试下一级,全程都判不了才返回 None
            continue
        determined = True
        if data and not provider.is_empty_tile(data):
            found = z
            break

    if found is None and determined:
        found = zmin          # 逐级都无影像(如公海),取最低级别兜底
    if found is None:
        return None           # 全程网络失败:判不了,不缓存

    if cache_hours > 0:
        import time as _time
        _probe_cache[ckey] = (found, _time.time())
    return found
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_esri_imagery_provider -v`
Expected: PASS,29 项通过

- [ ] **Step 5: 用真实网络抽验探测结果**

创建临时脚本 `scripts/_probe_levels.py`:

```python
"""抽验探测结果与设计 §3.11 的实测表是否一致。"""
import sys

sys.path.insert(0, ".")
from backend.config import EsriImageryConfig
from backend.providers.esri_imagery import probe_max_level

cfg = EsriImageryConfig(enabled=True, proxy="127.0.0.1:6789",
                        probe_cache_hours=0)

CASES = [("上海", (121.470, 31.225, 121.482, 31.235), 19),
         ("北京", (116.380, 39.995, 116.392, 40.005), 19),
         ("拉萨", (91.135, 29.645, 91.145, 29.655), 18),
         ("阿里", (80.105, 32.505, 80.115, 32.515), 17),
         ("可可西里", (91.995, 35.495, 92.005, 35.505), 17)]

for name, bbox, want in CASES:
    got = probe_max_level(bbox, cfg=cfg)
    flag = "OK" if got == want else f"不符(期望 {want})"
    print(f"{name:10} 探测 = {got}  {flag}")
```

Run: `.venv/Scripts/python.exe scripts/_probe_levels.py`
Expected:上海/北京 19、拉萨 18、阿里/可可西里 17。个别城市可能因服务更新而变化,记录实际值即可;**若全部返回 None,说明代理没走通** —— 回看 `_fetch_tile_bytes` 的 ProxyHandler。

删除脚本:`rm scripts/_probe_levels.py`

- [ ] **Step 6: 提交**

```bash
git add backend/providers/esri_imagery.py tests/test_esri_imagery_provider.py
git commit -m "feat(esri): 加区域最高级别探测,避免西部下到整片占位图

实测最高可用级别随地区变化:城市 z19、拉萨/乌鲁木齐 z18、
西藏青海新疆无人区仅 z17。沿用 terrain.probe_max_level 的同款模式。

与 terrain 版的关键差别:必须走代理。Terrain3D 直连可用故它用 urllib
直连,World Imagery 直连全超时,照抄会让探测永远失败并静默降级。

返回 None 表示判不了(网络问题),调用方应放行用户选的级别而非降级;
该情况不写缓存,免得代理刚起来时的一次失败粘住 24 小时。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 13: 估算与建议级别的 API 分流(含"大范围不被拒绝"护栏)

**取消硬上限后,预估是用户提交前唯一能看到规模的途径**(设计 §4.10)。必须如实报数且不拦截。

**Files:**
- Create: `tests/test_estimate_mercator.py`
- Modify: `backend/api/tasks.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_estimate_mercator.py`:

```python
"""估算与建议级别的网格分流。

最重要的一条是 test_huge_range_not_rejected:用户明确要求不设瓦片数上限
(设计 §9 Q1),这条护栏防止后人好心加回 MERCATOR_TILE_LIMIT。
"""
import unittest

from backend.api.tasks import _estimate_detail, _estimate_total
from backend.core.mercator_tiling import estimate_mercator_tiles

BBOX = (116.36, 39.98, 116.41, 40.03)      # 0.05 度


class TestEstimateTotal(unittest.TestCase):
    def test_mercator_image_uses_xyz_count(self):
        got = _estimate_total(BBOX, [18], "google_img")
        self.assertEqual(got, estimate_mercator_tiles(BBOX, [18]))
        self.assertEqual(got, 1862)

    def test_esri_imagery_same_grid_as_google(self):
        """两者网格同构,计数必须一致。"""
        self.assertEqual(_estimate_total(BBOX, [18], "esri_imagery"),
                         _estimate_total(BBOX, [18], "google_img"))

    def test_tianditu_still_uses_4326(self):
        """回归护栏:天地图计数不能被改成墨卡托。"""
        from backend.core.tiling import estimate_levels
        self.assertEqual(_estimate_total(BBOX, [18], "tianditu_img"),
                         estimate_levels(BBOX, [18]))

    def test_dem_unchanged(self):
        self.assertEqual(_estimate_total(BBOX, [16], "esri_terrain"),
                         estimate_mercator_tiles(BBOX, [16]))


class TestEstimateDetail(unittest.TestCase):
    def test_mercator_image_detail_shape(self):
        got = _estimate_detail(BBOX, [17, 18], "google_img")
        self.assertIn("levels", got)
        self.assertIn("total_tiles", got)
        self.assertIn("total_bytes", got)
        self.assertEqual(len(got["levels"]), 2)
        row = got["levels"][0]
        for k in ("z", "tiles", "bytes", "cols", "rows", "width", "height"):
            self.assertIn(k, row, k)

    def test_totals_are_sum_of_levels(self):
        got = _estimate_detail(BBOX, [17, 18], "google_img")
        self.assertEqual(got["total_tiles"],
                         sum(r["tiles"] for r in got["levels"]))

    def test_pixel_dims_match_tile_grid(self):
        got = _estimate_detail(BBOX, [18], "google_img")
        row = got["levels"][0]
        self.assertEqual(row["width"], row["cols"] * 256)
        self.assertEqual(row["height"], row["rows"] * 256)

    def test_image_avg_bytes_smaller_than_dem(self):
        """影像 jpg 比 DEM 的 LERC 小得多,估算体积不该套用 DEM 的经验值。"""
        img = _estimate_detail(BBOX, [16], "google_img")
        dem = _estimate_detail(BBOX, [16], "esri_terrain")
        self.assertLess(img["total_bytes"], dem["total_bytes"])


class TestNoTileLimit(unittest.TestCase):
    """★ 回归护栏 ★ 用户明确要求不设单任务瓦片数上限(设计 §9 Q1)。

    实现时**不要**加回 MERCATOR_TILE_LIMIT 这类拦截。规模由预估如实
    呈现、由用户判断;要限制的话该做的是任务分块或调度优先级,不是拒绝提交。
    """

    def test_huge_range_not_rejected(self):
        """3 度选区 z21 约 4 亿张:必须正常返回预估,不抛异常。"""
        big = (115.0, 38.5, 118.0, 41.5)
        got = _estimate_total(big, [21], "google_img")
        self.assertGreater(got, 300_000_000)

    def test_huge_range_detail_not_rejected(self):
        big = (115.0, 38.5, 118.0, 41.5)
        got = _estimate_detail(big, [20, 21], "google_img")
        self.assertGreater(got["total_tiles"], 400_000_000)
        self.assertGreater(got["total_bytes"], 0)

    def test_no_limit_constant_exists(self):
        """确认代码里没有这个常量。"""
        import backend.api.tasks as mod
        self.assertFalse(hasattr(mod, "MERCATOR_TILE_LIMIT"))
        import backend.core.mercator_tiling as mt
        self.assertFalse(hasattr(mt, "MERCATOR_TILE_LIMIT"))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_estimate_mercator -v`
Expected: FAIL — `test_mercator_image_uses_xyz_count` 报值不符(当前 `google_img` 走的是 4326 计数)

- [ ] **Step 3: 改 `backend/api/tasks.py` 的估算分流**

先在 import 段补充:

```python
from ..core.formats import GEO_MERCATOR, grid_of
from ..core.mercator_tiling import estimate_mercator_tiles, suggest_mercator_levels
from ..providers.esri_imagery import is_esri_imagery_provider
from ..providers.google import is_google_provider
```

把 `_estimate_total` 改为:

```python
def _estimate_total(bbox, levels: list[int], provider: str) -> int:
    """按数据源的**网格**选择瓦片计数方式。

    墨卡托(Google/Esri 影像、Esri DEM)用 XYZ 计数,天地图用 4326。
    刻意不在这里做任何上限检查:用户明确要求不设单任务瓦片数上限
    (设计 §9 Q1),规模由调用方如实呈现、由用户判断。
    """
    if grid_of(provider) == GEO_MERCATOR:
        return estimate_mercator_tiles(bbox, levels)
    return estimate_levels(bbox, levels)
```

在 `_DEM_AVG_BYTES` 之后加影像的经验值:

```python
# 墨卡托影像单瓦片平均字节数。实测 Google/Esri 的 jpg 在 9~27KB 之间,
# 取 18KB 作估算基准(设计 §3.8)。比 DEM 的 LERC 小得多,不能套用后者。
_MERC_IMG_AVG_BYTES = 18 * 1024
```

把 `_estimate_detail` 改为:

```python
def _estimate_detail(bbox, levels: list[int], provider: str) -> dict:
    """预估明细。墨卡托源逐层按 XYZ 网格计数;天地图走 4326 明细。

    ⚠️ 取消瓦片数硬上限后(设计 §9 Q1),**这是用户提交前唯一能看到规模的
    途径** —— 3 度选区 z21 是 5.3 亿张瓦片、约 9.6 TB。如实返回,不拦截。
    """
    if grid_of(provider) != GEO_MERCATOR:
        return estimate_levels_detail(bbox, levels, provider)
    is_dem = is_dem_provider(provider)
    avg = _DEM_AVG_BYTES if is_dem else _MERC_IMG_AVG_BYTES
    w, s, e, n = bbox
    per = []
    total_tiles = 0
    for z in sorted(set(levels)):
        tr = mercator_range_for_bbox(w, s, e, n, z)
        tiles = tr.count
        per.append({"z": z, "tiles": tiles, "bytes": tiles * avg,
                    "cols": tr.cols, "rows": tr.rows,
                    "width": tr.cols * DEM_TILE_SIZE,
                    "height": tr.rows * DEM_TILE_SIZE})
        total_tiles += tiles
    return {"levels": per, "total_tiles": total_tiles,
            "total_bytes": total_tiles * avg}
```

- [ ] **Step 4: 改级别上限与建议级别的分流**

在 `_parse_levels` 附近加一个取上限的辅助函数:

```python
def _z_cap_for(provider: str) -> int:
    """该数据源的服务级最高级别(前端下拉的上限)。

    注意这只是**服务级天花板**。Esri 的实际可用级别随地区变化,
    由 /api/tasks/imagery_max_level 按选区探测(设计 §3.11)。
    """
    if is_dem_provider(provider):
        return DEM_LAYERS[provider][2]
    if is_google_provider(provider):
        return int(settings.google.max_zoom)
    if is_esri_imagery_provider(provider):
        return int(settings.esri_imagery.max_zoom)
    return 18          # 天地图


def _z_floor_for(provider: str) -> int:
    """该数据源的最低级别。DEM 从 0 起,影像从 1 起(z0 盖全球,对影像无意义)。"""
    return 0 if is_dem_provider(provider) else 1
```

把 `api_estimate` 里的这两行:

```python
    dem = is_dem_provider(provider)
    z_cap = DEM_LAYERS[provider][2] if dem else 18
    lv = _parse_levels(levels, z_min, z_max, z_cap, z_floor=0 if dem else 1)
```

改为:

```python
    z_cap = _z_cap_for(provider)
    lv = _parse_levels(levels, z_min, z_max, z_cap,
                       z_floor=_z_floor_for(provider))
```

把 `api_suggest_levels` 的函数体改为:

```python
    from ..core.tiling import suggest_levels

    bbox = (west, south, east, north)
    if is_dem_provider(provider):
        # DEM 是墨卡托 XYZ 网格,且预算/级别下限与影像不同
        from ..core.dem_tiling import suggest_dem_levels
        return await asyncio.to_thread(
            suggest_dem_levels, bbox, DEM_LAYERS[provider][2])
    if grid_of(provider) == GEO_MERCATOR:
        # 墨卡托影像:级别从 1 起、预算 8000(见 suggest_mercator_levels 的说明)
        return await asyncio.to_thread(
            suggest_mercator_levels, bbox, _z_cap_for(provider))
    return await asyncio.to_thread(suggest_levels, bbox, 18, 1)
```

- [ ] **Step 5: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_estimate_mercator -v`
Expected: PASS,12 项通过

- [ ] **Step 6: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿

- [ ] **Step 7: 提交**

```bash
git add backend/api/tasks.py tests/test_estimate_mercator.py
git commit -m "feat(api): 估算与建议级别按网格分流,影像用 18KB 均值

墨卡托影像走 XYZ 计数与 suggest_mercator_levels;DEM 的 60KB 经验值
不适用于 jpg 影像,另给 18KB(实测 9~27KB)。

刻意不做任何瓦片数上限检查(设计 §9 Q1):取消硬上限后预估是用户
提交前唯一能看到规模的途径,必须如实返回。test_estimate_mercator 有
护栏盯着这件事。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 14: 代理连通性诊断与提交前预检

代理不通若留给 worker,表现是"任务跑起来又全部瓦片失败" —— 进度条停在 0% 然后红字失败,真正原因埋在日志里,还白占一个 worker 槽位数分钟。

**Files:**
- Modify: `backend/api/tools.py`
- Modify: `backend/api/tasks.py`
- Create: `tests/test_provider_precheck.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_provider_precheck.py`:

```python
"""提交前的代理预检。

要点:
  - 预检在**主进程**做,不留给 worker
  - 判不了(超时)时**放行**,不能把网络抖动当成"代理不通"而拒绝提交
  - 错误文案要含"改完之后怎么办"(配置项名 + 需重启)
"""
import unittest

from backend.api.tasks import _precheck_network_provider


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class TestPrecheck(unittest.TestCase):
    def _patch_probe(self, result):
        """result: True=通, False=不通, None=判不了。"""
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda provider, timeout=6: result
        return orig, mod

    def test_reachable_passes(self):
        orig, mod = self._patch_probe(True)
        try:
            ok, msg = _run(_precheck_network_provider("google_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_unreachable_rejected_with_actionable_message(self):
        orig, mod = self._patch_probe(False)
        try:
            ok, msg = _run(_precheck_network_provider("google_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertFalse(ok)
        # 文案必须告诉用户改哪里、以及改完要重启
        self.assertIn("google.proxy", msg)
        self.assertIn("重启", msg)

    def test_esri_message_points_to_its_own_config(self):
        orig, mod = self._patch_probe(False)
        try:
            ok, msg = _run(_precheck_network_provider("esri_imagery"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertFalse(ok)
        self.assertIn("esri_imagery.proxy", msg)

    def test_undetermined_passes(self):
        """判不了要放行 —— 与 probe_max_level 返回 None 时的取舍一致。
        把网络抖动当成"代理不通"会让用户在能下的时候也提交不了。"""
        orig, mod = self._patch_probe(None)
        try:
            ok, msg = _run(_precheck_network_provider("google_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)

    def test_tianditu_skipped(self):
        """天地图直连可用,不该被预检拦(也不该白跑一次网络请求)。"""
        called = []
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda p, timeout=6: called.append(p)
        try:
            ok, _msg = _run(_precheck_network_provider("tianditu_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(called, [])

    def test_dem_skipped(self):
        """Esri Terrain3D 直连可用,同样跳过。"""
        called = []
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda p, timeout=6: called.append(p)
        try:
            ok, _msg = _run(_precheck_network_provider("esri_terrain"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(called, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_provider_precheck -v`
Expected: FAIL — `ImportError: cannot import name '_precheck_network_provider'`

- [ ] **Step 3: 在 `backend/api/tasks.py` 加预检**

在 `_probe_dem_max_level` 附近插入:

```python
#: 需要代理的数据源(实测直连不通)。天地图与 Esri Terrain3D 直连可用,不在此列。
def _needs_proxy_provider(provider: str) -> bool:
    return is_google_provider(provider) or is_esri_imagery_provider(provider)


def _proxy_config_key(provider: str) -> str:
    """该数据源的代理配置项名(用于错误文案,让用户知道改哪里)。"""
    if is_google_provider(provider):
        return "google.proxy"
    if is_esri_imagery_provider(provider):
        return "esri_imagery.proxy"
    return ""


def _probe_provider_reachable(provider: str, timeout: int = 6) -> bool | None:
    """取一张低级别瓦片判连通性。

    返回 True=通,False=不通,None=判不了(超时等)。

    取真实瓦片而非 HEAD 根域名:端点失效(旧 khms 端点 404)与代理不通是两种
    不同故障,只有真正请求一张瓦片才能区分 —— 而这正是用户最需要区分的两种
    情况(前者改 url_template,后者开代理)。

    用 z2 的瓦片:世界级,任何时候都该有数据,不受选区位置影响。
    """
    import urllib.error
    import urllib.request

    if is_google_provider(provider):
        p = build_google_provider(provider, settings.google)
    elif is_esri_imagery_provider(provider):
        p = build_esri_imagery_provider(settings.esri_imagery)
    else:
        return True

    url = p.tile_url(1, 1, 2)
    proxy = p.proxy
    try:
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        req = urllib.request.Request(url, headers=p.headers)
        with opener.open(req, timeout=timeout) as resp:
            return resp.status == 200 and bool(resp.read(64))
    except urllib.error.HTTPError:
        # 明确的 HTTP 错误(404/403):端点问题,不是代理不通
        return False
    except Exception:
        # 连不上/超时:区分不了"代理没开"与"网络抖动",交给调用方按放行处理
        return None


async def _precheck_network_provider(provider: str) -> tuple[bool, str]:
    """提交前预检:该数据源现在能不能取到瓦片。返回 (是否放行, 错误文案)。

    为什么在主进程做:留给 worker 的话,代理不通表现为"任务变 running →
    瓦片逐张重试 → 全部失败",用户看到进度条停在 0% 然后红字失败,真正原因
    (代理没开)埋在日志里,还白占一个 worker 槽位数分钟。

    **判不了时放行**:与 probe_max_level 返回 None 时的取舍一致
    (providers/terrain.py:129)。不要把网络抖动当成"代理不通"而拒绝提交。
    """
    if not _needs_proxy_provider(provider):
        return True, ""
    reachable = await asyncio.to_thread(_probe_provider_reachable, provider)
    if reachable is not False:
        return True, ""           # True 或 None 都放行
    key = _proxy_config_key(provider)
    proxy = (settings.google.proxy if is_google_provider(provider)
             else settings.esri_imagery.proxy)
    where = f"代理 {proxy}" if proxy else "直连(未配置代理)"
    return False, (
        f"该数据源当前取不到瓦片({where})。"
        f"这两个源实测直连不通,必须配置 HTTP 代理:"
        f"请确认代理软件已启动,并检查 config.yaml 的 {key}。"
        f"注意修改配置后需**重启服务**才会生效。"
        f"若代理正常,则可能是端点已变更,请检查对应的 url_template。")
```

并在 import 段补充 provider 构造器:

```python
from ..providers.esri_imagery import (build_esri_imagery_provider,
                                      is_esri_imagery_provider)
from ..providers.google import build_google_provider, is_google_provider
```

- [ ] **Step 4: 在 `api_create_task` 接入预检**

找到 `api_create_task` 中校验参数、创建任务之前的位置,插入:

```python
    # Google/Esri 需要代理才能下载。不预检的话失败会推迟到 worker 里,
    # 表现为"任务跑起来又全部瓦片失败",且白占一个 worker 槽位数分钟。
    ok, msg = await _precheck_network_provider(data.provider)
    if not ok:
        raise HTTPException(400, msg)
```

- [ ] **Step 5: 在 `backend/api/tools.py` 加代理诊断**

在文件末尾(诊断路由之前)加:

```python
async def _diagnose_network_source(name: str, cfg, provider_key: str) -> dict:
    """探测一个需代理的数据源:未启用则跳过,启用则实际取一张瓦片。

    与 _diagnose_one(本机可执行文件)并列但实现不同:这里是网络请求。
    按症状给可操作的 hint —— 用户最需要区分的是"代理没开"与"端点变了"。
    """
    from ..api.tasks import _probe_provider_reachable
    from ..providers.base import normalize_proxy

    item = {"name": name, "enabled": bool(cfg.enabled), "proxy": "",
            "reachable": False, "error": "", "hint": ""}
    if not cfg.enabled:
        item["hint"] = "未启用(config.yaml 中设 enabled: true 后可用)"
        return item
    proxy = normalize_proxy(cfg.proxy)
    item["proxy"] = proxy or "(直连)"
    state = await asyncio.to_thread(_probe_provider_reachable, provider_key, 6)
    if state is True:
        item["reachable"] = True
        return item
    if state is None:
        item["error"] = "连接超时"
        item["hint"] = ("代理不可连接或网络不通。确认代理软件已启动;"
                        "该源实测直连不通,必须配代理。改配置后需重启服务。")
    else:
        item["error"] = "端点返回错误(非 200)"
        item["hint"] = "代理可能正常但端点已变更,请检查 url_template。"
    return item
```

在诊断路由的返回体里追加这两项(找到 `/api/tools/diagnose` 的处理函数,在组装结果处加):

```python
    result["google"] = await _diagnose_network_source(
        "Google 影像", settings.google, "google_img")
    result["esri_imagery"] = await _diagnose_network_source(
        "Esri World Imagery", settings.esri_imagery, "esri_imagery")
```

- [ ] **Step 6: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_provider_precheck -v`
Expected: PASS,6 项通过

- [ ] **Step 7: 手工验证预检(设计清单第 5 条)**

关掉代理软件,提交一个 `google_img` 任务。

Expected:立即收到 400 错误,文案含 `google.proxy` 与"重启服务",**不是**创建任务后失败。

重开代理,再提交 → 正常创建。

- [ ] **Step 8: 运行全量测试并提交**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿

```bash
git add backend/api/tasks.py backend/api/tools.py tests/test_provider_precheck.py
git commit -m "feat(api): 提交前代理预检与诊断接口

预检放主进程:留给 worker 的话代理不通会表现为"任务跑起来又全部瓦片
失败",真正原因埋在日志里,还白占一个 worker 槽位数分钟。

判不了(超时)时放行,与 probe_max_level 返回 None 的取舍一致 ——
不把网络抖动当成代理不通而拒绝提交。

错误文案含配置项名与"需重启服务":spawn 下运行中的 worker 不会重载
config.yaml,用户改完立刻重试可能仍用旧值。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 15: `imagery_max_level` 接口与 Esri 级别剔除

**Files:**
- Modify: `backend/api/tasks.py`
- Create: `tests/test_imagery_level_trim.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_imagery_level_trim.py`:

```python
"""Esri 区域级别剔除。

关键取舍:**剔除而非拒绝**。用户在西藏选了 z15-z18,z18 无数据时,
下 z15-z17 是合理的期望,不该整个任务被拒。
"""
import unittest

from backend.api.tasks import _trim_levels_for_region


class TestTrimLevels(unittest.TestCase):
    def test_trims_above_probed_max(self):
        kept, dropped = _trim_levels_for_region([15, 16, 17, 18], 17)
        self.assertEqual(kept, [15, 16, 17])
        self.assertEqual(dropped, [18])

    def test_nothing_dropped_when_all_available(self):
        kept, dropped = _trim_levels_for_region([15, 16, 17], 19)
        self.assertEqual(kept, [15, 16, 17])
        self.assertEqual(dropped, [])

    def test_none_probe_keeps_all(self):
        """判不了(网络问题)时放行全部,不能误判成"该范围没影像"而砍级别。"""
        kept, dropped = _trim_levels_for_region([15, 16, 17, 18, 19], None)
        self.assertEqual(kept, [15, 16, 17, 18, 19])
        self.assertEqual(dropped, [])

    def test_never_returns_empty(self):
        """全部超限时保留最低一级,避免产出一个零级别的空任务。"""
        kept, dropped = _trim_levels_for_region([18, 19], 17)
        self.assertEqual(kept, [18])
        self.assertEqual(dropped, [19])

    def test_preserves_order_and_dedups(self):
        kept, _dropped = _trim_levels_for_region([17, 15, 16, 15], 19)
        self.assertEqual(kept, [15, 16, 17])

    def test_empty_input(self):
        kept, dropped = _trim_levels_for_region([], 18)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_imagery_level_trim -v`
Expected: FAIL — `ImportError: cannot import name '_trim_levels_for_region'`

- [ ] **Step 3: 加剔除函数与接口**

在 `backend/api/tasks.py` 加:

```python
def _trim_levels_for_region(levels: list[int],
                            max_level: int | None) -> tuple[list[int], list[int]]:
    """按区域实际最高级别剔除超限级别。返回 (保留, 被剔除)。

    **剔除而非拒绝**:用户在西藏选了 z15-z18,z18 无数据时下 z15-z17 是
    合理的期望,不该整个任务被拒。被剔除的级别要回报给用户(任务详情里
    记明"已跳过 z18(该区域最高 z17)"),否则就是静默丢功能。

    max_level 为 None(判不了)时保留全部 —— 与 probe_max_level 的返回值
    语义一致,不把网络问题当成"该范围没影像"。
    """
    uniq = sorted(set(int(z) for z in levels))
    if max_level is None or not uniq:
        return uniq, []
    kept = [z for z in uniq if z <= max_level]
    dropped = [z for z in uniq if z > max_level]
    if not kept:
        # 全部超限:保留最低一级,避免产出零级别的空任务
        kept = [uniq[0]]
        dropped = uniq[1:]
    return kept, dropped


async def _probe_imagery_max_level(bbox, provider: str) -> int | None:
    """探测影像数据源在该范围的最高可用级别;None 表示判不了。

    只有 Esri 需要:实测 Google 陆地处处可到 z21,无地区性降级。
    """
    if not is_esri_imagery_provider(provider):
        return None
    if not settings.esri_imagery.probe_max_zoom:
        return None
    from ..providers.esri_imagery import probe_max_level
    # 逐级网络请求,必须放线程里(与 _probe_dem_max_level 一致)
    return await asyncio.to_thread(probe_max_level, bbox,
                                   settings.esri_imagery)
```

在 `api_dem_max_level` 之后加新路由:

```python
@router.get("/imagery_max_level")
async def api_imagery_max_level(west: float, south: float, east: float,
                                north: float, provider: str = "esri_imagery"):
    """探测影像数据源在该范围的最高可用级别(前端据此禁用超限级别)。

    实测 Esri World Imagery 各区域最高级别不同:城市 z19、拉萨/乌鲁木齐 z18、
    西藏青海新疆无人区仅 z17(z18 即整片占位图)。不探测的话用户选 z18
    在西部会下到一整片灰色。

    Google 不需要探测(陆地处处可到 z21),对它返回 service_max 即可。
    max_level 为 null 表示探测失败(网络/代理问题),前端此时**不应禁用**
    任何级别 —— 与 dem_max_level 的既有约定一致。
    """
    if not (is_esri_imagery_provider(provider) or is_google_provider(provider)):
        raise HTTPException(400, "该数据源不支持影像级别探测")
    service_max = _z_cap_for(provider)
    if is_google_provider(provider):
        # 无地区性降级,直接返回服务上限,省掉一次网络往返
        return {"provider": provider, "max_level": service_max,
                "service_max": service_max, "probed": False}
    max_level = await _probe_imagery_max_level(
        (west, south, east, north), provider)
    return {"provider": provider, "max_level": max_level,
            "service_max": service_max, "probed": True}
```

- [ ] **Step 4: 在 `api_create_task` 接入剔除**

在预检之后、`create_task` 之前插入:

```python
    # Esri 各区域最高级别不同(西部无人区仅 z17)。超限级别会整片下到占位图,
    # 故提交时剔除 —— 剔除而非拒绝,并把结果回报给用户。
    dropped_levels: list[int] = []
    if is_esri_imagery_provider(data.provider) and data.levels:
        probed = await _probe_imagery_max_level(
            (data.west, data.south, data.east, data.north), data.provider)
        kept, dropped_levels = _trim_levels_for_region(data.levels, probed)
        if dropped_levels:
            data.levels = kept
            logger.info("任务[%s] 剔除超出该区域能力的级别 %s(区域最高 %s)",
                        data.name, dropped_levels, probed)
```

并在创建成功后的返回体里带上这个信息(找到 `api_create_task` 的 return,追加字段):

```python
    if dropped_levels:
        resp["dropped_levels"] = dropped_levels
        resp["dropped_reason"] = (
            f"该区域 Esri 影像最高仅到 z{max(data.levels)},"
            f"已跳过 {', '.join('z' + str(z) for z in dropped_levels)}")
```

> 注意:`resp` 是现有 return 的字典变量名,按实际代码调整。若现有 return 是直接构造字典,先赋给 `resp` 再返回。

- [ ] **Step 5: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_imagery_level_trim -v`
Expected: PASS,6 项通过

- [ ] **Step 6: 手工验证(设计清单第 15 条)**

Run(服务已启动):
```bash
curl "http://127.0.0.1:8000/api/tasks/imagery_max_level?west=121.470&south=31.225&east=121.482&north=31.235&provider=esri_imagery"
curl "http://127.0.0.1:8000/api/tasks/imagery_max_level?west=80.105&south=32.505&east=80.115&north=32.515&provider=esri_imagery"
```
Expected:上海返回 `max_level: 19`,阿里返回 `max_level: 17`。

再对阿里选区提交一个勾了 z18 的 Esri 任务 → 返回体应含 `dropped_levels: [18]`。

- [ ] **Step 7: 运行全量测试并提交**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿

```bash
git add backend/api/tasks.py tests/test_imagery_level_trim.py
git commit -m "feat(api): 加 imagery_max_level 接口与 Esri 区域级别剔除

实测 Esri 各区域最高级别不同(城市 z19、西部无人区仅 z17),
用户选 z18 在西部会下到一整片灰色占位图。

剔除而非拒绝:西藏选 z15-z18 时下 z15-z17 是合理期望,不该整个
任务被拒;被剔除的级别回报给用户,避免静默丢功能。

探测判不了(None)时保留全部级别,不把网络问题当成"该范围没影像"。
Google 不探测(陆地处处可到 z21),直接返回服务上限省一次往返。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

# 阶段四:四种导出格式按网格分流

墨卡托源的拼接图是 3857,而现有导出阶段假定源是 4326。分流对照(设计 §4.6):

| 阶段 | geodetic(现状) | mercator(新增) |
|---|---|---|
| TMS 导出 | `export_tms`(无损直映射) | `export_tms_from_source`(重采样,因 TMS 是 4326 网格) |
| OSM 导出 | `export_osm`(4326→3857 重采样) | `export_osm`(源已是 3857,恒等变换) |
| MBTiles | `pack_mbtiles(scheme="tms")` | `pack_mbtiles(scheme="xyz")` |

## Task 16: TMS 与 OSM 阶段按网格取源

**Files:**
- Create: `tests/test_export_grid_routing.py`
- Modify: `backend/core/runner.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_export_grid_routing.py`:

```python
"""导出阶段的网格分流。

只验证"选对了哪条路径/参数",不跑真实切片(那在手工验收里做)。
"""
import unittest

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import _mbtiles_scheme_for, _tms_needs_resample


class TestTmsRouting(unittest.TestCase):
    def test_geodetic_uses_direct_mapping(self):
        """天地图 4326 与 gdal2tiles geodetic 网格同构,无损直映射。"""
        self.assertFalse(_tms_needs_resample(GEO_GEODETIC))

    def test_mercator_needs_resample(self):
        """TMS 是 4326 网格;3857 源必须重采样,不能直映射。"""
        self.assertTrue(_tms_needs_resample(GEO_MERCATOR))


class TestMbtilesScheme(unittest.TestCase):
    def test_geodetic_is_tms_scheme(self):
        self.assertEqual(_mbtiles_scheme_for(GEO_GEODETIC, "tms"), "tms")

    def test_osm_dir_is_always_xyz(self):
        """osm 目录的行号自北向南,恒为 xyz 约定,与源网格无关。"""
        self.assertEqual(_mbtiles_scheme_for(GEO_GEODETIC, "osm"), "xyz")
        self.assertEqual(_mbtiles_scheme_for(GEO_MERCATOR, "osm"), "xyz")

    def test_tms_dir_is_always_tms(self):
        """tms 目录的行号自南向北,恒为 tms 约定。"""
        self.assertEqual(_mbtiles_scheme_for(GEO_MERCATOR, "tms"), "tms")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_export_grid_routing -v`
Expected: FAIL — `ImportError: cannot import name '_tms_needs_resample'`

- [ ] **Step 3: 在 `runner.py` 加两个分流辅助函数**

在 `_crs_for_grid` 之后插入:

```python
def _tms_needs_resample(grid: str) -> bool:
    """TMS 导出是否需要重采样(而非无损直映射)。

    TMS 用的是 gdal2tiles geodetic 网格(EPSG:4326):
      - geodetic 源(天地图):与之同构,可无损直映射(export_tms)
      - mercator 源(Google/Esri):网格不同构,必须重采样
        (export_tms_from_source,以拼接图为源逐瓦片 reproject)
    """
    return grid == GEO_MERCATOR


def _mbtiles_scheme_for(grid: str, stage_key: str) -> str:
    """打包 MBTiles 时的行号约定。

    取决于**瓦片目录本身**的约定,与源网格无关:
      - tms/ 目录:行号自南向北 => "tms"
      - osm/ 目录:行号自北向南 => "xyz"
    grid 参数保留是为了让调用点显式表明"已考虑过网格",避免后人误以为漏了。
    """
    return "xyz" if stage_key == "osm" else "tms"
```

- [ ] **Step 4: 改 `_stage_tms` 按网格取源**

在 `_stage_tms` 函数开头,把决定"能否直映射"的判断改为同时考虑网格。找到该函数里调用 `export_tms` 与 `export_tms_from_source` 的分支,在其之前加:

```python
    # 墨卡托源(Google/Esri)的瓦片与 TMS 的 geodetic 网格不同构,
    # 不能用 export_tms 的无损直映射 —— 那会把 3857 瓦片当 4326 瓦片摆放,
    # 产出整体错位的瓦片包。必须以拼接图为源重采样。
    force_resample = _tms_needs_resample(ctx.grid)
```

然后把原有"是否走直映射"的条件与 `force_resample` 合并 —— 即原先走 `export_tms` 的分支,加上 `and not force_resample`;`force_resample` 为真时走 `export_tms_from_source`(与本地源已有的那条路径相同)。

> 具体合并位置随现有代码结构而定。原则:**mercator 源一律走 `export_tms_from_source`**,源图取 `{name}_z{max}.tif`(阶段 geotiff 的产出,此时是 3857)。

- [ ] **Step 5: 确认 `_stage_osm` 对 3857 源可用**

`export_osm` 以 WarpedVRT 做重投影到 3857。源已是 3857 时是恒等变换,GDAL 会优化,结果正确(设计 §4.6 的取舍:一期沿用,不加快路径)。

在 `_stage_osm` 里给源坐标系加一句注释说明:

```python
    # OSM 源:geodetic 源是 4326、mercator 源是 3857,两者都能喂给 export_osm
    # (它内部用 WarpedVRT 重投影到 3857;源已是 3857 时为恒等变换,GDAL 会优化)。
    # 一期不加"3857 直映射"快路径,控制改动面;若实测性能不可接受再补(设计 §4.6)。
```

- [ ] **Step 6: 改 MBTiles 打包的 scheme**

找到 `runner.py` 里调用 `pack_mbtiles(...)` 的位置,把硬编码的 `scheme=` 改为:

```python
                    scheme=_mbtiles_scheme_for(ctx.grid, key),
```

- [ ] **Step 7: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_export_grid_routing -v`
Expected: PASS,5 项通过

- [ ] **Step 8: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿(天地图的 TMS/OSM 路径未变)

- [ ] **Step 9: 提交**

```bash
git add backend/core/runner.py tests/test_export_grid_routing.py
git commit -m "feat(runner): TMS/OSM/MBTiles 导出按网格分流

TMS 用 gdal2tiles geodetic 网格:天地图 4326 源可无损直映射,
墨卡托源必须以拼接图为源重采样 —— 否则会把 3857 瓦片当 4326 摆放,
产出整体错位的瓦片包。

OSM 一期沿用 export_osm(3857 源是恒等变换,GDAL 会优化),
不加直映射快路径以控制改动面。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 17: 阶段四导出格式验收

- [ ] **Step 1: 四格式全勾提交**

提交任务:`google_img`,北京 0.01° 范围,级别 z15~z17,导出勾选**全部四项**(合并 GeoTIFF、TMS 瓦片、OSM 瓦片、MBTiles 容器)。

Expected:四个阶段全部 done,无 failed。

- [ ] **Step 2: 检查目录结构**

Run:
```bash
.venv/Scripts/python.exe -c "
from pathlib import Path
import glob
for d in glob.glob('output/*'):
    p = Path(d)
    if not (p / 'tms').exists() and not (p / 'osm').exists():
        continue
    print('==', p.name)
    for sub in ('tms', 'osm'):
        s = p / sub
        if s.exists():
            lv = sorted(x.name for x in s.iterdir() if x.is_dir())
            print(f'  {sub}/ 级别目录 = {lv}')
    for mb in p.glob('*.mbtiles'):
        print('  mbtiles =', mb.name, mb.stat().st_size, 'bytes')
"
```
Expected:`tms/` 下是 gdal 级目录(源 z15~17 对应 L14~16)、`osm/` 下是 z15~17。

- [ ] **Step 3: 验证 TMS 瓦片坐标正确(重采样路径)**

用 QGIS 加载 `tms/tilemapresource.xml`(或直接看某张瓦片对应的地理位置),确认与底图对齐。

> **这一步最容易出问题**:若 TMS 走了直映射(没走重采样),瓦片会整体错位。错位表现为图在纬度方向被拉伸/偏移。

- [ ] **Step 4: 验证 MBTiles 可读**

Run:
```bash
.venv/Scripts/python.exe -c "
import glob, sqlite3
for f in glob.glob('output/*/*.mbtiles'):
    con = sqlite3.connect(f)
    n = con.execute('SELECT count(*) FROM tiles').fetchone()[0]
    meta = dict(con.execute('SELECT name, value FROM metadata').fetchall())
    print(f, 'tiles =', n, 'format =', meta.get('format'), 'bounds =', meta.get('bounds'))
    con.close()
"
```
Expected:瓦片数 > 0,bounds 是经纬度范围(MBTiles 规范要求 WGS84)。

- [ ] **Step 5: 对 Esri 源重复 Step 1-4**

数据源换 `esri_imagery`,级别 z15~z17(北京该区域 z19 可用,z17 稳妥)。

- [ ] **Step 6: 补验证记录并提交**

在 `docs/验证记录-google-esri影像.md` 追加:

```markdown
## 阶段四:四种导出格式

| 格式 | 数据源 | 结果 | 备注 |
|---|---|---|---|
| 合并 GeoTIFF | google_img | (填) | crs=(填) |
| TMS 瓦片目录 | google_img | (填) | 级别目录=(填),QGIS 对齐=(填) |
| OSM 瓦片目录 | google_img | (填) | 级别目录=(填) |
| MBTiles | google_img | (填) | 瓦片数=(填),bounds=(填) |
| 四格式 | esri_imagery | (填) | |
```

```bash
git add docs/验证记录-google-esri影像.md
git commit -m "docs: 补阶段四(四种导出格式)验收记录

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

# 阶段五:预览端点与前端接入

## Task 18: 预览瓦片转发端点

**这个端点跑在主进程里**,而下载跑在 worker —— 是**两个进程**。下载器的信号量对它完全无效,而两者共用同一个代理出口。必须独立限流 + 短超时,否则会从主进程这一侧破坏进程隔离改造保住的性质。

**Files:**
- Create: `backend/api/tiles.py`
- Create: `tests/test_preview_tiles.py`
- Modify: `backend/main.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_preview_tiles.py`:

```python
"""预览瓦片转发端点。

关注点:
  - SSRF 护栏:只接受已登记且 enabled 的 provider key
  - 失败降级:返回透明 PNG 而非 5xx(否则地图破图)
  - 不写缓存:预览与下载用途不同,混用会让下载进度估算失真
"""
import unittest

from backend.api import tiles as tiles_api


def _run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


class TestProviderWhitelist(unittest.TestCase):
    def test_unknown_provider_rejected(self):
        """防止端点变成任意 URL 代理(SSRF)。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            tiles_api._resolve_preview_provider("../../etc/passwd")
        self.assertEqual(cm.exception.status_code, 404)

    def test_tianditu_not_allowed_here(self):
        """天地图底图前端直连(有自己的 token 机制),不走本端点。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            tiles_api._resolve_preview_provider("tianditu_img")

    def test_disabled_provider_rejected(self):
        from fastapi import HTTPException
        from backend.config import settings
        orig = settings.google.enabled
        settings.google.enabled = False
        try:
            with self.assertRaises(HTTPException) as cm:
                tiles_api._resolve_preview_provider("google_img")
            self.assertEqual(cm.exception.status_code, 403)
        finally:
            settings.google.enabled = orig

    def test_enabled_google_accepted(self):
        from backend.config import settings
        from backend.providers.google import GoogleProvider
        orig = settings.google.enabled
        settings.google.enabled = True
        try:
            p = tiles_api._resolve_preview_provider("google_img")
            self.assertIsInstance(p, GoogleProvider)
        finally:
            settings.google.enabled = orig

    def test_enabled_esri_accepted(self):
        from backend.config import settings
        from backend.providers.esri_imagery import EsriImageryProvider
        orig = settings.esri_imagery.enabled
        settings.esri_imagery.enabled = True
        try:
            p = tiles_api._resolve_preview_provider("esri_imagery")
            self.assertIsInstance(p, EsriImageryProvider)
        finally:
            settings.esri_imagery.enabled = orig


class TestTransparentFallback(unittest.TestCase):
    def test_placeholder_is_valid_png(self):
        png = tiles_api._TRANSPARENT_PNG
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_placeholder_is_tiny(self):
        """1x1 透明 PNG,避免每次失败都传一大坨。"""
        self.assertLess(len(tiles_api._TRANSPARENT_PNG), 200)


class TestLimits(unittest.TestCase):
    def test_preview_concurrency_below_download(self):
        """预览并发必须小于下载并发:争抢代理时该让下载优先。"""
        from backend.config import settings
        self.assertLess(tiles_api._PREVIEW_CONCURRENCY,
                        settings.download.concurrency)

    def test_preview_timeout_much_shorter_than_download(self):
        """预览超时必须远小于下载超时:主进程要服务所有 HTTP 与 WS,
        挂 30s 的转发会拖垮交互 —— 那正是进程隔离要解决的问题。"""
        from backend.config import settings
        self.assertLess(tiles_api._PREVIEW_TIMEOUT, settings.download.timeout)
        self.assertLessEqual(tiles_api._PREVIEW_TIMEOUT, 10)


class TestNoCaching(unittest.TestCase):
    def test_module_does_not_touch_cache_dir(self):
        """预览不写缓存(设计 D6):混用会让下载进度估算失真。"""
        import inspect
        src = inspect.getsource(tiles_api)
        self.assertNotIn("cache_dir", src)
        self.assertNotIn("tile_path", src)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_preview_tiles -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.api.tiles'`

- [ ] **Step 3: 创建 `backend/api/tiles.py`**

```python
"""底图预览瓦片转发(Google / Esri World Imagery)。

为什么需要转发:浏览器无法使用后端的代理配置,而这两个源直连不通。

⚠️ **本端点跑在主进程**,而下载跑在 worker 子进程 —— 是两个进程。
这带来三条必须遵守的约束:

1. **独立限流**。下载器的 asyncio.Semaphore 在 worker 进程里,对本端点
   完全无效。两者共用同一个本机代理出口:下载峰值 num_workers ×
   download.concurrency(默认 2×8=16),OpenLayers 平移一次又能并发
   20+ 张。不独立限流会把代理打满。

2. **短超时**。download.timeout 默认 30s,这里绝不能用 —— 主进程的事件
   循环要服务所有 HTTP 与 WebSocket,20 个挂 30s 的转发请求会拖垮交互,
   那正是进程隔离改造要解决的问题,不该从这里漏回来。

3. **复用单个 session**。每请求新建 ClientSession 会重建连接池与代理隧道
   (https 要重新 CONNECT 握手),预览这种高频小请求下开销显著。

另外:**不写缓存**(设计 D6)。预览与下载用途不同(预览要即时、可失败;
下载要完整、可续传),混用缓存会让下载的进度估算失真。
"""
from __future__ import annotations

import asyncio

import aiohttp
from fastapi import APIRouter, HTTPException, Response

from ..config import settings
from ..core.logs import logger
from ..providers.esri_imagery import (build_esri_imagery_provider,
                                      is_esri_imagery_provider)
from ..providers.google import build_google_provider, is_google_provider

router = APIRouter(prefix="/api/tiles", tags=["tiles"])

#: 预览转发的并发上限。刻意小于 download.concurrency —— 预览是"看一眼",
#: 下载是"要完整成果";争抢代理时应当让下载优先。
#: 这个信号量是主进程模块级单例,与 worker 里下载器的信号量是两个独立的闸,
#: 二者之和才是代理的实际峰值压力(设计预算:16 + 4 = 20)。
_PREVIEW_CONCURRENCY = 4

#: 预览取瓦片超时(秒)。短:拿不到就返回占位图让地图继续可用,
#: 而不是让用户对着转圈的瓦片等 30 秒。
_PREVIEW_TIMEOUT = 8

_sem = asyncio.Semaphore(_PREVIEW_CONCURRENCY)
_session: aiohttp.ClientSession | None = None

#: 1x1 全透明 PNG。取不到瓦片时返回它,避免地图出现破图图标。
_TRANSPARENT_PNG = bytes([
    0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A, 0x00, 0x00, 0x00, 0x0D,
    0x49, 0x48, 0x44, 0x52, 0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01,
    0x08, 0x06, 0x00, 0x00, 0x00, 0x1F, 0x15, 0xC4, 0x89, 0x00, 0x00, 0x00,
    0x0A, 0x49, 0x44, 0x41, 0x54, 0x78, 0x9C, 0x63, 0x00, 0x01, 0x00, 0x00,
    0x05, 0x00, 0x01, 0x0D, 0x0A, 0x2D, 0xB4, 0x00, 0x00, 0x00, 0x00, 0x49,
    0x45, 0x4E, 0x44, 0xAE, 0x42, 0x60, 0x82,
])


async def startup() -> None:
    """建预览用的共享 session(由 main.py 的 lifespan 调用)。

    必须在运行中的事件循环里创建 —— ClientSession 会绑定创建时的 loop,
    模块导入期(无 loop)创建会在首次使用时报
    "Timeout context manager should be used inside a task"。

    这里只统一超时,**不设 session 级 proxy**:不同 provider 可能配不同代理
    (google.proxy 与 esri_imagery.proxy 是两个字段),故 proxy 逐请求传。
    这与下载器"session 级 proxy"的选择相反,理由也相反:下载器一个 session
    只服务一个 provider(重试时必须一致),本端点一个 session 服务所有 provider。
    """
    global _session
    if _session is None:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=_PREVIEW_TIMEOUT))


async def shutdown() -> None:
    """关闭 session。应在 task_queue.shutdown() **之前**调用 ——
    后者会 await to_thread 做数十秒的 join,放在它后面关会让 session
    悬着那么久(日志出现 Unclosed client session)。"""
    global _session
    if _session is not None:
        await _session.close()
        _session = None


def _resolve_preview_provider(provider: str):
    """把 provider key 解析为实例;不在白名单或未启用则抛 HTTP 错误。

    只接受已登记且 enabled 的 key —— 否则这个端点就成了任意 URL 代理
    (SSRF)。天地图不走这里:它由前端直连(有自己的 token 机制)。
    """
    if is_google_provider(provider):
        if not settings.google.enabled:
            raise HTTPException(403, "Google 影像未启用(config.yaml 中设 google.enabled: true)")
        return build_google_provider(provider, settings.google)
    if is_esri_imagery_provider(provider):
        if not settings.esri_imagery.enabled:
            raise HTTPException(403, "Esri World Imagery 未启用(config.yaml 中设 esri_imagery.enabled: true)")
        return build_esri_imagery_provider(settings.esri_imagery)
    raise HTTPException(404, f"不支持预览的数据源:{provider}")


@router.get("/{provider}/{z}/{x}/{y}")
async def api_preview_tile(provider: str, z: int, x: int, y: int):
    """转发一张预览瓦片。失败时返回透明占位图,不破坏地图渲染。"""
    p = _resolve_preview_provider(provider)
    if _session is None:
        # lifespan 未跑(如单测直接调路由):降级为占位图而非 500
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")

    url = p.tile_url(x, y, z)
    try:
        async with _sem:
            async with _session.get(url, proxy=p.proxy) as resp:
                if resp.status != 200:
                    return Response(content=_TRANSPARENT_PNG,
                                    media_type="image/png")
                data = await resp.read()
                ctype = resp.headers.get("Content-Type", "image/jpeg")
    except Exception as e:
        # 代理不通/超时:返回占位图。不记 error 级日志 —— 预览失败是常态
        # (代理波动、平移太快),刷屏的日志会盖掉真正的问题。
        logger.debug("预览瓦片取失败 %s z=%s x=%s y=%s:%s", provider, z, x, y, e)
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")

    if not data:
        return Response(content=_TRANSPARENT_PNG, media_type="image/png")
    # 浏览器侧缓存 1 小时:预览不写服务端缓存(设计 D6),但让浏览器缓存
    # 能显著减少平移时的重复请求与代理压力。
    return Response(content=data, media_type=ctype,
                    headers={"Cache-Control": "public, max-age=3600"})
```

- [ ] **Step 4: 在 `backend/main.py` 挂载与接入生命周期**

在 import 段加:

```python
from .api import tiles as tiles_api
```

在路由注册处加:

```python
app.include_router(tiles_api.router)
```

在 `lifespan` 里,`task_queue.start()` 之后加:

```python
    # 预览瓦片转发用的共享 HTTP session(必须在事件循环里创建)
    await tiles_api.startup()
```

在 `finally:` 块里,**在 `await task_queue.shutdown()` 之前**加:

```python
        # 先关预览 session:task_queue.shutdown() 会 join worker 数十秒,
        # 放它后面会让 session 悬着那么久(Unclosed client session 警告)
        await tiles_api.shutdown()
```

- [ ] **Step 5: 运行测试**

Run: `.venv/Scripts/python.exe -m unittest tests.test_preview_tiles -v`
Expected: PASS,11 项通过

- [ ] **Step 6: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿

- [ ] **Step 7: 手工验证端点**

启动服务后:

```bash
curl -s -o /tmp/t.jpg -w "%{http_code} %{size_download}\n" \
  "http://127.0.0.1:8000/api/tiles/google_img/16/53955/24810"
```
Expected:`200 18084`(或相近大小)

未启用时:
```bash
curl -s -w "%{http_code}\n" -o /dev/null \
  "http://127.0.0.1:8000/api/tiles/tianditu_img/16/1/1"
```
Expected:`404`

- [ ] **Step 8: 提交**

```bash
git add backend/api/tiles.py backend/main.py tests/test_preview_tiles.py
git commit -m "feat(api): 加底图预览瓦片转发端点(主进程,独立限流)

浏览器无法使用后端代理配置,故经后端转发。

本端点在主进程、下载在 worker,是两个进程:下载器的信号量对它无效,
而两者共用同一个代理出口(下载峰值 2x8=16,预览另给 4)。超时定 8s
而非 download.timeout 的 30s —— 主进程要服务所有 HTTP 与 WS,挂 30s
的转发会拖垮交互,那正是进程隔离要解决的问题。

session 复用但 proxy 逐请求传(一个 session 服务多个 provider,
各自代理可能不同),与下载器的选择相反。

只接受已登记且 enabled 的 provider key,防止变成任意 URL 代理。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 19: 前端数据源下拉与级别常量

**Files:**
- Modify: `frontendvue/src/utils/taskDefaults.js`
- Modify: `frontendvue/src/components/ProcessDialog.vue`
- Modify: `frontendvue/src/components/RedownloadDialog.vue`
- Modify: `frontendvue/src/components/TaskDetail.vue`
- Modify: `frontendvue/src/utils/taskDefaults.test.js`

- [ ] **Step 1: 写失败测试**

在 `frontendvue/src/utils/taskDefaults.test.js` 末尾追加:

```javascript
// ---------- Google / Esri 影像的级别常量 ----------

test('GOOGLE_LEVELS 覆盖 1-21', () => {
  assert.equal(GOOGLE_LEVELS[0], 1)
  assert.equal(GOOGLE_LEVELS[GOOGLE_LEVELS.length - 1], 21)
  assert.equal(GOOGLE_LEVELS.length, 21)
})

test('ESRI_IMAGERY_LEVELS 覆盖 1-19', () => {
  assert.equal(ESRI_IMAGERY_LEVELS[0], 1)
  assert.equal(ESRI_IMAGERY_LEVELS[ESRI_IMAGERY_LEVELS.length - 1], 19)
  assert.equal(ESRI_IMAGERY_LEVELS.length, 19)
})

test('levelsForProvider 按数据源给出级别列表', () => {
  assert.deepEqual(levelsForProvider('tianditu_img'), IMG_LEVELS)
  assert.deepEqual(levelsForProvider('google_img'), GOOGLE_LEVELS)
  assert.deepEqual(levelsForProvider('google_road'), GOOGLE_LEVELS)
  assert.deepEqual(levelsForProvider('esri_imagery'), ESRI_IMAGERY_LEVELS)
  assert.deepEqual(levelsForProvider('esri_terrain'), DEM_LEVELS)
})

test('levelsForProvider 未知数据源回落影像级别', () => {
  assert.deepEqual(levelsForProvider('whatever'), IMG_LEVELS)
})

test('needsRegionProbe 只对 Esri 影像为真', () => {
  // Google 实测陆地处处可到 z21,无地区性降级,不必探测
  assert.equal(needsRegionProbe('google_img'), false)
  assert.equal(needsRegionProbe('esri_imagery'), true)
  assert.equal(needsRegionProbe('tianditu_img'), false)
  assert.equal(needsRegionProbe('esri_terrain'), false)
})
```

并把该文件顶部的 import 改为(补 5 个名字):

```javascript
import {
  DEM_CRS_HINT,
  DEM_LEVELS,
  ESRI_IMAGERY_LEVELS,
  GOOGLE_LEVELS,
  IMG_LEVELS,
  levelsForProvider,
  needsRegionProbe,
  // ...(保留该文件原有的其余 import 名字)
} from './taskDefaults.js'
```

> 注意:原 import 列表里已有的名字要保留,只是**追加**上面几个。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: FAIL — `GOOGLE_LEVELS is not defined`

> 必须用 glob 直参。Node 24 下 `node --test src/` 目录直参会误报(见 CLAUDE.md)。

- [ ] **Step 3: 改 `frontendvue/src/utils/taskDefaults.js`**

在文件顶部的级别常量附近追加:

```javascript
// Google 影像级别。实测陆地处处可到 z21(含拉萨/乌鲁木齐等西部城市),
// z22 仅部分地区有 —— 21 是全球陆地可用的临界值。
export const GOOGLE_LEVELS = Array.from({ length: 21 }, (_, i) => i + 1)

// Esri World Imagery 级别。19 是服务级天花板(亚欧城市实际上限;
// z20 仅美国境内有)。注意**实际可用级别随地区变化**:西藏/青海/新疆
// 无人区最高仅 z17,由后端 /api/tasks/imagery_max_level 按选区探测,
// 前端据结果禁用超限级别(见 needsRegionProbe)。
export const ESRI_IMAGERY_LEVELS = Array.from({ length: 19 }, (_, i) => i + 1)

/** 按数据源给出可选级别列表。 */
export function levelsForProvider(provider) {
  if (provider === 'esri_terrain' || provider === 'aws_terrain') return DEM_LEVELS
  if (provider === 'esri_imagery') return ESRI_IMAGERY_LEVELS
  if (typeof provider === 'string' && provider.startsWith('google_')) return GOOGLE_LEVELS
  return IMG_LEVELS
}

/**
 * 该数据源是否需要按选区探测最高可用级别。
 *
 * 只有 Esri World Imagery 需要:实测它各区域最高级别不同(城市 z19、
 * 拉萨 z18、西部无人区仅 z17),不探测的话用户选 z18 在西部会下到
 * 一整片灰色占位图。Google 实测无地区性降级,不必探测。
 */
export function needsRegionProbe(provider) {
  return provider === 'esri_imagery'
}
```

- [ ] **Step 4: 改三个组件的下拉与显示名**

`ProcessDialog.vue` 的 `providerOptions`(约 46 行)追加 5 项:

```javascript
  { value: 'google_img', label: 'Google 卫星影像', group: '影像' },
  { value: 'google_hybrid', label: 'Google 影像(含路网)', group: '影像' },
  { value: 'google_road', label: 'Google 路线图', group: '影像' },
  { value: 'google_terrain', label: 'Google 地形', group: '影像' },
  { value: 'esri_imagery', label: 'Esri World Imagery', group: '影像' },
```

`RedownloadDialog.vue` 的 `providerOptions`(约 24 行)追加同样 5 项。

`TaskDetail.vue` 的显示名映射(约 25 行)追加:

```javascript
  google_img: 'Google 卫星影像',
  google_hybrid: 'Google 影像(含路网)',
  google_road: 'Google 路线图',
  google_terrain: 'Google 地形',
  esri_imagery: 'Esri World Imagery',
```

- [ ] **Step 5: 改 `ProcessDialog.vue` 的级别列表来源**

把(约 158 行):

```javascript
const levelList = computed(() => (isDem.value ? DEM_LEVELS : IMG_LEVELS))
```

改为:

```javascript
// 级别列表按数据源取:Google 到 21、Esri 影像到 19、天地图 18、DEM 0-16
const levelList = computed(() => levelsForProvider(form.provider))
```

并把该文件的 import 补上 `levelsForProvider`(与 `DEM_LEVELS, IMG_LEVELS` 同处)。

- [ ] **Step 6: 运行前端测试**

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: PASS(含新增 5 项)

- [ ] **Step 7: 提交**

```bash
git add frontendvue/src/utils/taskDefaults.js frontendvue/src/utils/taskDefaults.test.js frontendvue/src/components/ProcessDialog.vue frontendvue/src/components/RedownloadDialog.vue frontendvue/src/components/TaskDetail.vue
git commit -m "feat(frontend): 数据源下拉加 5 项影像源,级别列表按源取

Google 到 z21(实测陆地处处可用),Esri 影像到 z19(亚欧城市上限)。
Esri 的实际可用级别随地区变化,needsRegionProbe 标记它需要探测。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 20: 前端底图接入与区域级别禁用

**Files:**
- Modify: `frontendvue/src/utils/basemap.js`
- Modify: `frontendvue/src/utils/basemap.test.js`
- Modify: `frontendvue/src/composables/useMap.js`
- Modify: `frontendvue/src/api.js`
- Modify: `frontendvue/src/components/ProcessDialog.vue`

- [ ] **Step 1: 写失败测试**

在 `frontendvue/src/utils/basemap.test.js` 末尾追加:

```javascript
test('BASEMAP_OPTIONS 含 Google 与 Esri 影像', () => {
  const vals = BASEMAP_OPTIONS.map((x) => x.value)
  assert.ok(vals.includes('google_img'))
  assert.ok(vals.includes('esri_imagery'))
})

test('Google/Esri 底图标记为经后端转发', () => {
  for (const v of ['google_img', 'esri_imagery']) {
    const opt = BASEMAP_OPTIONS.find((x) => x.value === v)
    assert.equal(opt.viaBackend, true, `${v} 应标记 viaBackend`)
  }
})

test('天地图底图不经后端转发(前端直连,有自己的 token)', () => {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === 'tianditu_img')
  assert.ok(!opt.viaBackend)
})

test('Google 底图可缩放到 21', () => {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === 'google_img')
  assert.equal(opt.zoomable, true)
  assert.equal(opt.maxZoom, 21)
})

test('Esri 影像底图上限 19', () => {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === 'esri_imagery')
  assert.equal(opt.maxZoom, 19)
})

test('basemapTileUrl 给出后端转发地址', () => {
  assert.equal(basemapTileUrl('google_img'),
    '/api/tiles/google_img/{z}/{x}/{y}')
  assert.equal(basemapTileUrl('esri_imagery'),
    '/api/tiles/esri_imagery/{z}/{x}/{y}')
})

test('basemapTileUrl 对天地图返回 null(不走转发)', () => {
  assert.equal(basemapTileUrl('tianditu_img'), null)
})
```

并把该文件的 import 补上 `basemapTileUrl`。

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: FAIL — `basemapTileUrl is not a function`

- [ ] **Step 3: 改 `frontendvue/src/utils/basemap.js`**

在 `BASEMAP_OPTIONS` 数组末尾追加 5 项:

```javascript
  // Google / Esri 经后端转发(浏览器无法用后端的代理配置)。
  // types 为空数组:它们不是天地图 WMTS 图层组,由 useMap 走 XYZ 源构造。
  {
    value: 'google_img',
    label: 'Google 卫星影像',
    types: [],
    removable: true,
    zoomable: true,
    maxZoom: 21,
    viaBackend: true,
  },
  {
    value: 'google_hybrid',
    label: 'Google 影像(含路网)',
    types: [],
    removable: true,
    zoomable: true,
    maxZoom: 21,
    viaBackend: true,
  },
  {
    value: 'google_road',
    label: 'Google 路线图',
    types: [],
    removable: true,
    zoomable: true,
    maxZoom: 21,
    viaBackend: true,
  },
  {
    value: 'google_terrain',
    label: 'Google 地形',
    types: [],
    removable: true,
    zoomable: true,
    maxZoom: 21,
    viaBackend: true,
  },
  {
    value: 'esri_imagery',
    label: 'Esri World Imagery',
    types: [],
    removable: true,
    zoomable: true,
    maxZoom: 19,
    viaBackend: true,
  },
```

在文件末尾追加:

```javascript
/**
 * 经后端转发的底图瓦片 URL 模板;不需要转发的返回 null。
 *
 * 后端转发的原因:浏览器无法使用后端的代理配置,而 Google/Esri 直连不通。
 * 天地图不走这里 —— 它由前端直连(有自己的 basemap_token 机制)。
 */
export function basemapTileUrl(value) {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === value)
  if (!opt || !opt.viaBackend) return null
  return `/api/tiles/${value}/{z}/{x}/{y}`
}

/** 底图的最大缩放级别;未声明时按天地图的 18。 */
export function basemapMaxZoom(value) {
  const opt = BASEMAP_OPTIONS.find((x) => x.value === value)
  return (opt && opt.maxZoom) || 18
}
```

- [ ] **Step 4: 改 `frontendvue/src/composables/useMap.js` 的 `setBasemap`**

在 `tiandituLayer` 函数之后加一个 XYZ 图层构造函数:

```javascript
  // 经后端转发的底图(Google / Esri)。走 /api/tiles/...,
  // 因此浏览器不直接接触这些站点,前端无需任何代理配置。
  function backendXyzLayer(providerKey) {
    return new TileLayer({
      source: new XYZ({
        url: basemapTileUrl(providerKey),
        crossOrigin: 'anonymous',
        maxZoom: basemapMaxZoom(providerKey),
      }),
    })
  }
```

把 `setBasemap` 改为按 provider 分派:

```javascript
  // 按用户选择/下载数据类型切换中间地图底图
  function setBasemap(providerKey) {
    baseKey = providerKey || 'tianditu_img'
    const backendUrl = basemapTileUrl(baseKey)
    // 移除旧底图组
    baseLayers.forEach((l) => map.removeLayer(l))
    if (backendUrl) {
      // Google / Esri:经后端转发,不需要天地图 token
      baseLayers = [backendXyzLayer(baseKey)]
    } else {
      // 天地图:需 token(未配置时只应用样式,保持原行为)
      if (!baseToken) { baseLayers = []; applyBasemapStyle(); return }
      const types = basemapTypesFor(baseKey)
      baseLayers = types.map((t) => tiandituLayer(t, baseToken))
    }
    applyBasemapStyle()
    // 插到最底层(矢量/预览层之下)
    baseLayers.forEach((l, i) => map.getLayers().insertAt(i, l))
  }
```

并把该文件顶部 `basemap.js` 的 import 补上两个新函数:

```javascript
import {
  basemapMaxZoom,
  basemapTileUrl,
  basemapTypesFor,
  basemapZIndexForLevel,
} from '../utils/basemap.js'
```

- [ ] **Step 5: 在 `frontendvue/src/api.js` 加探测接口**

在现有 `demMaxLevel`(约 87 行)旁边加:

```javascript
  imageryMaxLevel: ({ west, south, east, north, provider }) =>
    req(`/api/tasks/imagery_max_level?west=${west}&south=${south}&east=${east}&north=${north}&provider=${provider || 'esri_imagery'}`),
```

- [ ] **Step 6: 在 `ProcessDialog.vue` 接入级别禁用**

参照该文件现有的 DEM 探测逻辑(它已用 `demMaxLevel` 做级别禁用),加一份影像版:

```javascript
// Esri 影像各区域最高级别不同(城市 z19、西部无人区仅 z17)。
// 探测后禁用超限级别,避免用户选了 z18 却下到一整片灰色占位图。
const imageryMaxLevel = ref(null)

async function probeImageryMaxLevel() {
  if (!needsRegionProbe(form.provider)) { imageryMaxLevel.value = null; return }
  const bbox = drawStore.bbox
  if (!bbox) { imageryMaxLevel.value = null; return }
  try {
    const [west, south, east, north] = bbox
    const r = await api.imageryMaxLevel({
      west, south, east, north, provider: form.provider,
    })
    // max_level 为 null 表示探测失败(网络/代理问题)——
    // 此时不禁用任何级别,与 DEM 探测的既有约定一致
    imageryMaxLevel.value = r.max_level
  } catch {
    imageryMaxLevel.value = null
  }
}

// 某级别是否因超出该区域能力而不可选
function levelDisabled(z) {
  if (imageryMaxLevel.value == null) return false
  return z > imageryMaxLevel.value
}
```

把 `needsRegionProbe` 加入该文件从 `taskDefaults.js` 的 import;在数据源切换与选区变化的 watch 里调用 `probeImageryMaxLevel()`(与现有 DEM 探测的触发点并列)。

在级别勾选的模板处,给超限级别加 `:disabled="levelDisabled(z)"` 与提示文案,例如:

```html
<t-checkbox
  v-for="z in levelList"
  :key="z"
  :value="z"
  :disabled="levelDisabled(z)"
>
  z{{ z }}<span v-if="levelDisabled(z)" class="lv-hint">(该区域无数据)</span>
</t-checkbox>
```

> 具体标签名与属性按该文件现有的级别勾选写法调整,不要引入新的 UI 库用法。

- [ ] **Step 7: 运行前端测试**

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: PASS(含新增 7 项)

- [ ] **Step 8: 构建前端**

Run: `cd frontendvue && npm run build`
Expected: 构建成功,产物在 `frontendvue/dist`

- [ ] **Step 9: 提交**

```bash
git add frontendvue/src/utils/basemap.js frontendvue/src/utils/basemap.test.js frontendvue/src/composables/useMap.js frontendvue/src/api.js frontendvue/src/components/ProcessDialog.vue
git commit -m "feat(frontend): 底图接入 Google/Esri 并按区域禁用超限级别

底图走 /api/tiles 后端转发(浏览器用不了后端的代理配置),故前端
无需任何代理配置。setBasemap 改为按 provider 分派:天地图走 WMTS
构造(不变),Google/Esri 走 XYZ 源指向转发端点。

Esri 的级别上限来自按选区探测而非常量:实测西部无人区最高仅 z17。
探测返回 null(网络问题)时不禁用任何级别,与 DEM 探测的约定一致。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 20b: 裁剪支持 3857 拼接图

**自查发现的缺口。** `core/postprocess.py::clip_to_geometry` 的 docstring 明写"把 **EPSG:4326** 的 GeoTIFF 原地裁剪",它把 GeoJSON 几何(WGS84)的坐标**直接**当作源图坐标用。对 3857 拼接图,几何坐标(经纬度,数量级 ±180)与图的坐标(米,数量级 ±2e7)完全不在一个尺度上 —— `rio_mask` 会判成"几何与影像无重叠",**静默返回 False、图一点没裁**。

`core/tile_clip.py:38` 已有正确做法(`transform_geom("EPSG:4326", dst_crs, g)`),照它办。

**Files:**
- Create: `tests/test_clip_3857.py`
- Modify: `backend/core/postprocess.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_clip_3857.py`:

```python
"""裁剪对 3857 源的支持。

背景:clip_to_geometry 原先假定源是 EPSG:4326,把 WGS84 几何坐标直接当源图
坐标用。对 3857 拼接图(坐标是米,±2e7 量级),几何(±180 量级)会被判成
"与影像无重叠",静默返回 False —— 图一点没裁,用户看不出哪里错了。
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from backend.core.mercator_tiling import mercator_range_for_bbox, mosaic_bounds_3857
from backend.core.postprocess import clip_to_geometry


def _make_tif(path: Path, bounds, crs: str, size: int = 64) -> None:
    west, south, east, north = bounds
    profile = {"driver": "GTiff", "height": size, "width": size, "count": 3,
               "dtype": "uint8", "crs": crs,
               "transform": from_bounds(west, south, east, north, size, size)}
    with rasterio.open(path, "w", **profile) as dst:
        dst.write(np.full((3, size, size), 200, dtype=np.uint8))


class TestClip3857(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        # 选区:北京一小块;几何恒为 WGS84 经纬度(前端送来的就是这个)
        self.bbox = (116.36, 39.98, 116.41, 40.03)
        w, s, e, n = self.bbox
        # 取选区内部的一个小多边形,确保与图有重叠但不覆盖全图
        self.geom = {
            "type": "Polygon",
            "coordinates": [[[w + 0.01, s + 0.01], [e - 0.01, s + 0.01],
                             [e - 0.01, n - 0.01], [w + 0.01, n - 0.01],
                             [w + 0.01, s + 0.01]]],
        }

    def tearDown(self):
        self._tmp.cleanup()

    def test_clips_3857_source(self):
        """核心用例:3857 源必须真的被裁(返回 True)。"""
        tr = mercator_range_for_bbox(*self.bbox, 14)
        p = self.tmp / "merc.tif"
        _make_tif(p, mosaic_bounds_3857(tr), "EPSG:3857")
        self.assertTrue(clip_to_geometry(p, self.geom),
                        "3857 源未被裁剪 —— 几何很可能没做坐标转换")

    def test_3857_output_keeps_crs(self):
        tr = mercator_range_for_bbox(*self.bbox, 14)
        p = self.tmp / "merc2.tif"
        _make_tif(p, mosaic_bounds_3857(tr), "EPSG:3857")
        clip_to_geometry(p, self.geom)
        with rasterio.open(p) as ds:
            self.assertEqual(ds.crs.to_string(), "EPSG:3857")

    def test_3857_output_has_alpha(self):
        """与 4326 路径一致:裁剪后写显式 alpha 表达边界外。"""
        tr = mercator_range_for_bbox(*self.bbox, 14)
        p = self.tmp / "merc3.tif"
        _make_tif(p, mosaic_bounds_3857(tr), "EPSG:3857")
        clip_to_geometry(p, self.geom)
        with rasterio.open(p) as ds:
            self.assertGreaterEqual(ds.count, 4)

    def test_4326_behavior_unchanged(self):
        """回归护栏:天地图(4326)路径必须行为不变。"""
        p = self.tmp / "geo.tif"
        _make_tif(p, self.bbox, "EPSG:4326")
        self.assertTrue(clip_to_geometry(p, self.geom))
        with rasterio.open(p) as ds:
            self.assertEqual(ds.crs.to_string(), "EPSG:4326")
            self.assertGreaterEqual(ds.count, 4)

    def test_no_overlap_still_returns_false(self):
        """真正无重叠时仍要返回 False(不能因为加了转换就把它变成有重叠)。"""
        tr = mercator_range_for_bbox(*self.bbox, 14)
        p = self.tmp / "merc4.tif"
        _make_tif(p, mosaic_bounds_3857(tr), "EPSG:3857")
        far = {"type": "Polygon",
               "coordinates": [[[0.0, 0.0], [0.1, 0.0], [0.1, 0.1],
                                [0.0, 0.1], [0.0, 0.0]]]}
        self.assertFalse(clip_to_geometry(p, far))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_clip_3857 -v`
Expected: FAIL — `test_clips_3857_source` 报 `AssertionError: 3857 源未被裁剪`(`clip_to_geometry` 返回了 False)

- [ ] **Step 3: 改 `backend/core/postprocess.py::clip_to_geometry`**

把 docstring 第一行改为:

```python
    """把 GeoTIFF 原地裁剪到 geometry 边界并写显式 alpha 波段。

    geometry 恒为 WGS84 GeoJSON(前端送来的就是这个)。源图坐标系可以是
    EPSG:4326(天地图)或 EPSG:3857(Google/Esri 影像)—— 后者必须先把几何
    转到源图坐标系,否则经纬度(±180)与米(±2e7)不在一个尺度上,rio_mask
    会判成"无重叠"、**静默返回 False 而图一点没裁**。
    做法与 core/tile_clip.py:38 一致。
```

把 `geoms = _geojson_geometries(geometry)` 之后、`with rasterio.open(...)` 之前改为:

```python
    geoms = _geojson_geometries(geometry)
    if not geoms:
        return False

    # 几何是 WGS84;源图可能是 3857。必须先转到源图坐标系 —— 否则
    # rio_mask 判成无重叠,静默返回 False(图没裁,也没有任何报错)。
    with rasterio.open(src_path) as probe:
        src_crs = probe.crs
    if src_crs is not None and src_crs.to_string() != "EPSG:4326":
        from rasterio.warp import transform_geom
        geoms = [transform_geom("EPSG:4326", src_crs.to_string(), g)
                 for g in geoms]

    with rasterio.open(src_path) as src:
```

> 注意:原代码里 `with rasterio.open(src_path) as src:` 这一行**保留不动**,只是在它之前插入探测与转换。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_clip_3857 -v`
Expected: PASS,5 项通过

- [ ] **Step 5: 运行全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿(4326 路径行为未变)

- [ ] **Step 6: 手工验证裁剪**

提交一个 `google_img` 任务:用**多边形**(非矩形)画选区,勾"裁剪到选区",级别 z16,导出 GeoTIFF。

Expected:成果在多边形外透明,而非整幅矩形图。

> 若产出仍是完整矩形,说明转换没生效 —— 回看 Step 3。

- [ ] **Step 7: 提交**

```bash
git add backend/core/postprocess.py tests/test_clip_3857.py
git commit -m "fix(postprocess): 裁剪支持 3857 源,几何先转到源图坐标系

clip_to_geometry 原先假定源是 4326,把 WGS84 几何坐标直接当源图坐标。
对 3857 拼接图,经纬度(±180)与米(±2e7)不在一个尺度上,rio_mask 会
判成无重叠、静默返回 False —— 图一点没裁且无任何报错。

做法与 core/tile_clip.py:38 一致(transform_geom)。4326 路径不变。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## Task 21: 端到端验收(含进程隔离不回归)

**Files:**
- Modify: `docs/验证记录-google-esri影像.md`

- [ ] **Step 1: 全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 全绿。**确认 `test_scheduler` 与 `test_worker_dispatch` 是绿的** —— 它们红了说明改动侵入了进程隔离层。

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: 全绿

- [ ] **Step 2: 预览端到端(设计清单第 4 条)**

启动服务,在界面把底图切到 Google 卫星影像。

Expected:瓦片正常加载,可缩放到 z21。

再切到 Esri World Imagery → 正常加载。

关掉代理软件,平移地图 → **瓦片变透明/空白,地图仍可交互,不出现破图图标或报错弹窗**。

重开代理 → 瓦片恢复。

- [ ] **Step 3: 【关键】进程隔离不回归(设计清单第 8 条)**

提交一个 Google 大范围任务:范围 0.05°(约 5km),级别 z15~z18(约 2000+ 张瓦片),导出勾 GeoTIFF + TMS。

**任务运行期间**同时做以下操作,确认界面全程可用:

| 操作 | 期望 |
|---|---|
| 反复平移/缩放地图(预览持续请求) | 地图可交互,不卡死 |
| 打开任务列表 | 立即返回,不挂起 |
| 点进任务详情 | 立即返回 |
| 观察 WS 进度推送 | 进度持续更新,不中断 |
| 切换底图 | 正常切换 |

> 这是进程隔离改造要保住的核心性质。若预览转发写成同步或超时过长,会从主进程这一侧把它破坏掉。任一项挂起都要停下来查 `_PREVIEW_TIMEOUT` 与 `_PREVIEW_CONCURRENCY`。

- [ ] **Step 4: 预览不与下载抢代理(设计清单第 9 条)**

上一条运行中,观察预览瓦片是否**大面积**变占位图。

Expected:偶有个别瓦片空白可接受;大面积空白说明代理被下载打满。

若大面积空白,调整 `_PREVIEW_CONCURRENCY` / `_PREVIEW_TIMEOUT`,并把结论回写设计 §4.7 与 §9 Q3。

- [ ] **Step 5: 规模如实显示且不拦截(设计清单第 10 条)**

选 0.25° 范围(约 27km),勾 z15~z21。

Expected:
- 预估显示约 **369 万张**瓦片、约 66 MB(数字清晰可读);
- **可以正常提交,不被拒绝**(用户已决定不设上限)。

> 这条同时验证"不设上限"与"规模可见"。只满足前者(不拦但也不显示)是不可接受的。

提交后可立即暂停/删除该任务,不必真的下完。

- [ ] **Step 6: 配置重载语义(设计清单第 11 条)**

任务运行中修改 `config.yaml` 的 `google.proxy` 为一个错误地址。

Expected:**正在跑的任务不受影响**(spawn 下 worker 不重载配置)。

停止服务、重启、再提交 → 此时预检应报错,且文案含"重启服务"提示。

把配置改回正确值并重启。

- [ ] **Step 7: 建议级别(设计清单第 12 条)**

Run:
```bash
curl -s "http://127.0.0.1:8000/api/tasks/suggest_levels?west=116.36&south=39.98&east=116.41&north=40.03&provider=google_img" | python -m json.tool | head -20
curl -s "http://127.0.0.1:8000/api/tasks/suggest_levels?west=116.0&south=39.5&east=117.0&north=40.5&provider=google_img" | python -m json.tool | head -20
```
Expected:两者的 `recommended` 都**不含 z0**,`recommended_tiles` 在 8000 以内,且小范围的推荐级别更高。

- [ ] **Step 8: z19 真实细节抽验(设计清单第 15b 条)**

对上海选区(121.470~121.482 / 31.225~31.235)提交 `esri_imagery` 任务,级别 z18 与 z19 各一次。

Expected:z19 成果的细节明显多于 z18(这是把上限从 18 提到 19 的依据)。可用 QGIS 并排目视对比,或跑:

```bash
.venv/Scripts/python.exe -c "
import glob, numpy as np, rasterio
for f in sorted(glob.glob('output/*/*_z1[89].tif')):
    with rasterio.open(f) as ds:
        a = ds.read(1).astype(np.float32)
        g = float(np.abs(np.diff(a, axis=1)).mean())
        print(f'{f}  梯度={g:.2f}')
"
```
Expected:z19 的梯度不低于 z18(细节更丰富)。

- [ ] **Step 9: 代理瞬态故障频率(设计清单第 17 条)**

查看 Step 3 那个任务的日志,统计 `ClientConnectorError` / TLS 重置的出现次数。

Expected:偶发可接受。若失败率明显偏高(如超过瓦片总数的 1%),把 `download.max_retries` 调到 5 并把结论回写设计 §9 Q5。

- [ ] **Step 10: 补完验证记录**

在 `docs/验证记录-google-esri影像.md` 追加:

```markdown
## 阶段五:预览与端到端

| 项 | 结果 | 备注 |
|---|---|---|
| Google 底图预览 | (填) | 最高缩放=(填) |
| Esri 底图预览 | (填) | |
| 断代理降级为占位图 | (填) | 是否破图=(填) |
| 任务运行中地图可交互 | (填) | |
| 任务运行中 HTTP/WS 不挂起 | (填) | |
| 预览未被下载挤占 | (填) | 空白瓦片比例=(填) |
| 0.25° z15-21 预估 | (填) | 显示瓦片数=(填),是否可提交=(填) |
| 建议级别不含 z0 | (填) | |
| z19 细节优于 z18 | (填) | 梯度 z18=(填) z19=(填) |
| 代理瞬态故障频率 | (填) | 是否需调 max_retries=(填) |

## 参数校准结论

| 参数 | 设计暂定值 | 实测结论 |
|---|---|---|
| suggest 的 tile_budget | 8000 | (填) |
| _PREVIEW_CONCURRENCY | 4 | (填) |
| _PREVIEW_TIMEOUT | 8s | (填) |
| download.max_retries | 3 | (填) |

## 遗留问题

(填写)
```

若有参数需要调整,同步回写设计文档 §9 的"待实测校准的参数"表。

- [ ] **Step 11: 提交**

```bash
git add docs/验证记录-google-esri影像.md docs/superpowers/specs/2026-09-24-google-esri影像数据源-design.md
git commit -m "docs: 补阶段五端到端验收记录并回写参数校准结论

含进程隔离不回归验证:任务运行期间地图可交互、HTTP 与 WS 不挂起。

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>"
```

---

## 附:实现时容易踩的坑(汇总)

按"最容易造成静默错误"排序:

| # | 坑 | 后果 | 防线 |
|---|---|---|---|
| 1 | `is_empty_tile` 用"体量小 + 色值少"判据 | 真实深海瓦片被当无数据,成果出现**静默空洞**且瓦片不入缓存 | Task 7 的 `test_real_ocean_tile_not_empty` |
| 2 | Esri URL 写成 `{z}/{x}/{y}` | 取到地理位置错误的瓦片,**图像看着正常但坐标全错** | Task 7 的 `test_arcgis_order_is_z_y_x` |
| 3 | `probe_max_level` 照抄 terrain 的 urllib 直连 | 探测永远失败并**静默降级到最低级别** | Task 12 Step 5 的真实网络抽验 |
| 4 | 代理串不归一化 | `InvalidURL`(这个会明确报错,反而不算最坏) | Task 3 |
| 5 | TMS 对墨卡托源走直映射 | 瓦片包整体错位 | Task 16 的 `_tms_needs_resample` + Task 17 Step 3 |
| 5b | 裁剪不把几何转到 3857 | `rio_mask` 判成无重叠,**静默返回 False、图一点没裁** | Task 20b 的 `test_clips_3857_source` |
| 6 | 预览端点用 `download.timeout`(30s) | 主进程事件循环被拖垮,**回退到进程隔离前的老问题** | Task 18 的 `test_preview_timeout_much_shorter_than_download` |
| 7 | 预览 session 设 session 级 proxy | 两个 provider 配不同代理时其中一个必然走错 | Task 18 的 `startup()` 注释 |
| 8 | 好心加回瓦片数硬上限 | 违反用户决定(设计 §9 Q1) | Task 13 的 `TestNoTileLimit` |
| 9 | 改 `worker.py` / `scheduler.py` | 侵入进程隔离层 | Task 9 Step 10:两个测试文件必须保持绿 |
| 10 | 只改 `config.example.yaml` 忘了 `_CONFIG_TEMPLATE` | 新用户的 `config.yaml` 缺这两节 | Task 5 Step 3 |

---

## 附:与设计文档的对应关系

| 设计章节 | 对应 Task |
|---|---|
| §4.1 模块结构 | 全部 |
| §4.2 数据源登记 | Task 8 |
| §4.3 数据源实现 | Task 6、7、12 |
| §4.4 下载器改动(R1/D17) | Task 3、4 |
| §4.5 拼接改动(D3) | Task 2、20b(裁剪,计划自查补的缺口) |
| §4.6 执行管线分流(R6) | Task 9、11、16 |
| §4.7 预览端点(R2) | Task 18 |
| §4.8 配置(R3) | Task 5 |
| §4.9 错误处理(R4) | Task 14 |
| §4.10 规模与级别剔除(Q1) | Task 13、15 |
| §5.1 数据源下拉 | Task 19 |
| §5.2 底图预览 | Task 20 |
| §6.1 后端单测 | 各 Task 的测试步骤 |
| §6.3 手工验证清单 1-17 | Task 10、17、21 |
| §8 进程隔离边界 | Task 9 Step 10、Task 21 Step 3 |
| §9 Q1(不设上限) | Task 13 的 `TestNoTileLimit` |
| §9 Q4(Esri z19) | Task 5、19 |
| §9 Q2/Q3/Q5(待校准) | Task 21 Step 10 |

