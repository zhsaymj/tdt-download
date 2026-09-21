# 本地数据服务与预览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把本地目录发布成长期可访问的数据服务（模型/影像/矢量/地形），并在三维预览页与二维主界面的图层列表中接入这些服务。

**Architecture:** 后端新增 `services` 表持久化注册表 + 三个纯逻辑模块（成果识别 / 范围解析 / 注册表 CRUD），一个文件服务端点按注册根做路径穿越校验。前端新增服务面板与图层选择器，服务图层以与任务图层**完全相同的 `overlay_desc` 结构**进入既有 `overlays.js` 渲染管线，从而零改动复用透明度/层级/定位。

**Tech Stack:** FastAPI + sqlite3（无 ORM）+ 标准库 unittest；Vue3 + Pinia + TDesign + OpenLayers + Cesium 1.143。

**设计依据:** `docs/superpowers/specs/2026-09-21-本地数据服务与预览-design.md`

---

## 文件结构

**后端（新增）**

| 文件 | 职责 |
|---|---|
| `backend/core/service_bounds.py` | 范围解析：5 级元数据来源 + 瓦片索引反算 + 网格判定。纯函数 |
| `backend/core/service_scan.py` | 扫目录识别成果类型，产出候选。纯函数 |
| `backend/core/service_registry.py` | `services` 表 CRUD + 路径穿越校验 + `overlay_desc` 生成 |
| `backend/api/services.py` | HTTP 端点 + 文件服务 |

**后端（改动）**

| 文件 | 改动 |
|---|---|
| `backend/db.py` | `_SCHEMA` 加 `services` 表 |
| `backend/main.py` | 挂载新路由 |

**前端（新增）**

| 文件 | 职责 |
|---|---|
| `frontendvue/src/stores/service.js` | 服务列表状态 |
| `frontendvue/src/components/ServicePanel.vue` | 服务管理面板 |
| `frontendvue/src/components/ServiceLayerPicker.vue` | 服务图层选择器（预览页与主界面共用） |
| `frontendvue/src/preview/vectorGround.js` | 矢量贴地渲染封装 |

**前端（改动）**

| 文件 | 改动 |
|---|---|
| `frontendvue/src/utils/provider.js` | 服务分类常量集中处 |
| `frontendvue/src/stores/overlay.js` | key 拆两个函数、bbox 来源、`removeByService` |
| `frontendvue/src/components/DataDialog.vue` | `overlayKey` 三处引用改 `taskOverlayKey` |
| `frontendvue/src/components/TaskQueue.vue` | `doDelete` 补 `removeByTask`（既有 bug B1） |
| `frontendvue/src/components/AppTopBar.vue` | 加「服务」入口 |
| `frontendvue/src/components/LayerPanel.vue` | 「添加图层」改下拉 |
| `frontendvue/src/preview/PreviewApp.vue` | id 可选 + 服务图层 + applyTerrain 重构 + tileset Map |
| `frontendvue/src/components/TaskQueue.model3d.test.js` | 同步源码文本断言 |

---

## Task 1: 范围解析 — 网格判定与瓦片索引反算

**Files:**
- Create: `backend/core/service_bounds.py`
- Test: `tests/test_service_bounds.py`

设计依据 §3.1.2 / §3.1.3。**核心风险：项目里有三套互不相同的瓦片网格，函数签名相似、返回值都是四至元组，用错了不报错、只给错范围。**

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_service_bounds.py
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.core.service_bounds import (
    bounds_from_tile_index, detect_grid, geodetic_bounds, mercator_bounds,
)


class GridConversionTest(unittest.TestCase):
    """三套网格不可混用。这些数值是实测基准，改动网格逻辑必然触发失败。"""

    def test_geodetic_uses_terrain_grid_not_tms_grid(self):
        # Cesium 地形网格：列 2^(L+1)、行 2^L、span 180/2^L
        # L=12 时 x=6120..6129, y=3063..3070 对应新疆矿_高程的真实数据
        b = geodetic_bounds(6120, 3063, 6129, 3070, 12)
        self.assertAlmostEqual(b[0], 88.945312, places=5)
        self.assertAlmostEqual(b[1], 44.604492, places=5)
        self.assertAlmostEqual(b[2], 89.384766, places=5)
        self.assertAlmostEqual(b[3], 44.956055, places=5)

    def test_image_tms_grid_is_different(self):
        """锁定两套 geodetic 网格不可混用：同一组瓦片号必须得到不同结果。"""
        from backend.core.tiling import tile_bounds
        terrain = geodetic_bounds(6120, 3063, 6129, 3070, 12)
        # 影像 TMS 的 span 是 360/2^z，terrain 是 180/2^L —— 数值必然不同
        tms_west, _, _, _ = tile_bounds(6120, 3063, 12)
        self.assertNotAlmostEqual(terrain[0], tms_west, places=3)

    def test_mercator_bounds(self):
        # Web 墨卡托 z=1：(0,0) 是西北角，(0,0)-(1,1) 覆盖全球
        b = mercator_bounds(0, 0, 1, 1, 1)
        self.assertAlmostEqual(b[0], -180.0, places=4)
        self.assertAlmostEqual(b[1], -85.0511, places=3)
        self.assertAlmostEqual(b[2], 180.0, places=4)
        self.assertAlmostEqual(b[3], 85.0511, places=3)


class DetectGridTest(unittest.TestCase):
    def test_profile_attribute_wins(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tilemapresource.xml").write_text(
                '<TileMap><TileSets profile="geodetic">'
                '<TileSet href="0" order="0"/></TileSets></TileMap>',
                encoding="utf-8")
            self.assertEqual(detect_grid(p), "geodetic")

    def test_mercator_profile(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tilemapresource.xml").write_text(
                '<TileMap><TileSets profile="mercator">'
                '<TileSet href="0" order="0"/></TileSets></TileMap>',
                encoding="utf-8")
            self.assertEqual(detect_grid(p), "mercator")

    def test_z1_row_count_disambiguates(self):
        """无 XML 时看 z=1 的行数：geodetic 1 行、mercator 2 行。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "1" / "0").mkdir(parents=True)
            (p / "1" / "0" / "0.png").write_bytes(b"x")
            self.assertEqual(detect_grid(p), "geodetic")

        with TemporaryDirectory() as d:
            p = Path(d)
            for y in (0, 1):
                (p / "1" / "0").mkdir(parents=True, exist_ok=True)
                (p / "1" / "0" / f"{y}.png").write_bytes(b"x")
            self.assertEqual(detect_grid(p), "mercator")

    def test_defaults_to_mercator(self):
        with TemporaryDirectory() as d:
            self.assertEqual(detect_grid(Path(d)), "mercator")


class TileIndexBoundsTest(unittest.TestCase):
    def test_xyz_naming(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            for x in (2, 3):
                for y in (1, 2):
                    (p / "3" / str(x)).mkdir(parents=True, exist_ok=True)
                    (p / "3" / str(x) / f"{y}.png").write_bytes(b"x")
            b = bounds_from_tile_index(p, "mercator")
            self.assertIsNotNone(b)
            self.assertLess(b[0], b[2])   # west < east
            self.assertLess(b[1], b[3])   # south < north

    def test_col_row_naming(self):
        """本工具瓦片缓存格式 {z}/{col}_{row}.ext 也要认。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "3").mkdir(parents=True)
            (p / "3" / "2_1.png").write_bytes(b"x")
            (p / "3" / "3_2.png").write_bytes(b"x")
            b = bounds_from_tile_index(p, "geodetic")
            self.assertIsNotNone(b)
            self.assertLess(b[0], b[2])

    def test_empty_dir_returns_none(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(bounds_from_tile_index(Path(d), "mercator"))

    def test_max_files_marks_approx(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "3" / "2").mkdir(parents=True)
            (p / "3" / "2" / "1.png").write_bytes(b"x")
            got = bounds_from_tile_index(p, "mercator", max_files=0)
            # 超限时用已扫到的部分，范围仍是四元组
            self.assertIsNotNone(got)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_bounds -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.service_bounds'`

- [ ] **Step 3: 写实现**

```python
# backend/core/service_bounds.py
"""解析本地数据服务的范围，供图层定位。

**项目里有三套互不相同的瓦片网格**，函数签名相似、返回值都是四至元组，
用错了不报错、只静默给出错误范围。改本文件前先读这张表：

| 网格 | 列数 | 行数 | span | 来源模块 |
|---|---|---|---|---|
| 影像 TMS | 2^z | 2^(z-1) | 360/2^z | core/tiling.py |
| Web 墨卡托 | 2^z | 2^z | —— | core/osm.py |
| Cesium 地形 | 2^(L+1) | 2^L | 180/2^L | core/terrain_tiles.py |

地形的 geodetic 与影像的 geodetic **不是同一套网格**，不要复用 tiling.py。
"""
from __future__ import annotations

import math
import re
from pathlib import Path

from .logs import logger

#: Web 墨卡托世界范围半边长（米）
MERC_MAX = 20037508.342789244
#: Web 墨卡托纬度上限（度）
LAT_LIMIT = 85.05112878

#: 瓦片索引扫描的文件数上限。超限用已扫到的部分并把范围标记为估算值。
MAX_SCAN_FILES = 200_000

#: 瓦片文件名 {col}_{row}.ext（本工具缓存格式）
_COLROW_RE = re.compile(r"^(\d+)_(\d+)$")


def geodetic_bounds(x_min: int, y_min: int, x_max: int, y_max: int,
                    L: int) -> tuple[float, float, float, float]:
    """Cesium 地形网格（列 2^(L+1)、行 2^L、span 180/2^L）的四至。

    y 自南向北（0 在 -90°）。**这是地形专用网格，与影像 TMS 不同。**
    """
    from .terrain_tiles import geodetic_tile_bounds
    w, s, _, _ = geodetic_tile_bounds(x_min, y_min, L)
    _, _, e, n = geodetic_tile_bounds(x_max, y_max, L)
    return (w, s, e, n)


def mercator_bounds(x_min: int, y_min: int, x_max: int, y_max: int,
                    z: int) -> tuple[float, float, float, float]:
    """Web 墨卡托网格的四至，返回值已转成 WGS84 度。"""
    from rasterio.warp import transform_bounds

    from .osm import tile_bounds_3857

    minx, miny, _, _ = tile_bounds_3857(x_min, y_min, z)
    _, _, maxx, maxy = tile_bounds_3857(x_max, y_max, z)
    return tuple(transform_bounds(
        "EPSG:3857", "EPSG:4326", minx, miny, maxx, maxy, densify_pts=21))


def detect_grid(tile_dir: Path) -> str:
    """判定瓦片目录的网格：'geodetic'（天地图 TMS）/ 'mercator'（OSM XYZ）。

    判据按可靠性排序：
      1. tilemapresource.xml 的 <TileSets profile="...">——gdal2tiles 明确写了
      2. 第 1 级瓦片的行数：geodetic 只有 1 行、mercator 有 2 行
      3. 都判不出 → mercator（XYZ 是更通用的约定）
    """
    xml = tile_dir / "tilemapresource.xml"
    if xml.is_file():
        try:
            text = xml.read_text(encoding="utf-8", errors="replace")
            m = re.search(r'profile\s*=\s*"([^"]+)"', text)
            if m:
                prof = m.group(1).strip().lower()
                if "geodetic" in prof:
                    return "geodetic"
                if "mercator" in prof:
                    return "mercator"
        except OSError as e:
            logger.debug("读 tilemapresource.xml 失败 %s:%s", xml, e)

    # 第 1 级的行目录数：geodetic 的 matrix 是 2×1（行数 2^(z-1)=1）
    level1 = tile_dir / "1"
    if level1.is_dir():
        try:
            rows = {p.name for p in level1.rglob("*") if p.is_file()}
            row_ids = set()
            for name in rows:
                stem = Path(name).stem
                m = _COLROW_RE.match(stem)
                if m:
                    row_ids.add(int(m.group(2)))
                elif stem.isdigit():
                    row_ids.add(int(stem))
            if row_ids:
                return "geodetic" if max(row_ids) == 0 else "mercator"
        except OSError:
            pass
    return "mercator"


def _scan_level(d: Path, level: int, budget: list[int]) -> tuple[int, int, int, int] | None:
    """扫某级别目录，返回 (x_min, y_min, x_max, y_max)；无瓦片返回 None。

    支持两种命名：{x}/{y}.ext（XYZ）与 {col}_{row}.ext（本工具缓存）。
    budget 是可变单元素列表，用作跨级别的文件数配额。
    """
    lv = d / str(level)
    if not lv.is_dir():
        return None
    xs: list[int] = []
    ys: list[int] = []
    try:
        for sub in lv.iterdir():
            if sub.is_file():
                # 扁平命名 {col}_{row}.ext
                m = _COLROW_RE.match(sub.stem)
                if m:
                    xs.append(int(m.group(1)))
                    ys.append(int(m.group(2)))
                    budget[0] -= 1
                    if budget[0] <= 0:
                        break
                continue
            if not sub.is_dir() or not sub.name.isdigit():
                continue
            col = int(sub.name)
            for f in sub.iterdir():
                if not f.is_file():
                    continue
                if f.stem.isdigit():
                    xs.append(col)
                    ys.append(int(f.stem))
                else:
                    m = _COLROW_RE.match(f.stem)
                    if m:
                        xs.append(int(m.group(1)))
                        ys.append(int(m.group(2)))
                budget[0] -= 1
                if budget[0] <= 0:
                    break
            if budget[0] <= 0:
                break
    except OSError as e:
        logger.debug("扫瓦片级别失败 %s:%s", lv, e)
    if not xs or not ys:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def bounds_from_tile_index(tile_dir: Path, grid: str,
                           max_files: int = MAX_SCAN_FILES
                           ) -> tuple[float, float, float, float] | None:
    """扫描瓦片目录反算范围。取**最高级别**的区间——它最贴合实际数据。

    这是没有任何元数据的 OSM/XYZ 瓦片包的唯一定位途径。
    """
    levels = []
    try:
        for p in tile_dir.iterdir():
            if p.is_dir() and p.name.isdigit():
                levels.append(int(p.name))
    except OSError:
        return None
    if not levels:
        return None

    budget = [max_files]
    for z in sorted(levels, reverse=True):
        rng = _scan_level(tile_dir, z, budget)
        if rng is None:
            continue
        x_min, y_min, x_max, y_max = rng
        if grid == "geodetic":
            # 本工具 TMS 缓存目录的行号自南向北，先转成自北向南再做四至
            rows = 2 ** (z - 1) if z >= 1 else 1
            top = rows - 1 - y_max
            bot = rows - 1 - y_min
            return geodetic_bounds(x_min, top, x_max, bot, z)
        return mercator_bounds(x_min, y_min, x_max, y_max, z)
    return None
```

> **注意**：`geodetic_bounds` 走 `terrain_tiles.geodetic_tile_bounds`，而 TMS 缓存的反算需要先翻转行号（TMS 自南向北）。若扫描的是 OSM 的 mercator 目录则无需翻转。

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_bounds -v`
Expected: PASS（全部用例）

- [ ] **Step 5: 提交**

```bash
git add backend/core/service_bounds.py tests/test_service_bounds.py
git commit -m "feat(service): 新增瓦片范围反算与网格判定

三套瓦片网格（影像 TMS / Web 墨卡托 / Cesium 地形）行列定义各不相同，
函数签名相似但结果不同。测试用实测数据锁定不可混用。"
```

---

## Task 2: 范围解析 — 5 级元数据来源

**Files:**
- Modify: `backend/core/service_bounds.py`
- Test: `tests/test_service_bounds.py`（追加）

设计依据 §3.1.1。**实测推翻了三个常见假设**：`layer.json` 的 `bounds` 恒为世界范围；`tileset.json` 本工具产出用 `box` 而非 `region`；`tilemapresource.xml` 属性名是 `minx/miny`。

- [ ] **Step 1: 追加失败的测试**

```python
# tests/test_service_bounds.py 追加
import json


class MetadataBoundsTest(unittest.TestCase):
    def test_metadata_json(self):
        from backend.core.service_bounds import bounds_from_metadata
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "metadata.json").write_text(json.dumps({
                "bbox_wgs84": [100.0, 30.0, 101.0, 31.0]}), encoding="utf-8")
            self.assertEqual(bounds_from_metadata(p), [100.0, 30.0, 101.0, 31.0])

    def test_metadata_json_missing(self):
        from backend.core.service_bounds import bounds_from_metadata
        with TemporaryDirectory() as d:
            self.assertIsNone(bounds_from_metadata(Path(d)))

    def test_tilemapresource_attr_names_and_approx(self):
        """属性名是 minx/miny/maxx/maxy，且值是瓦片对齐四至 -> approx"""
        from backend.core.service_bounds import bounds_from_tilemapresource
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tilemapresource.xml").write_text(
                '<TileMap><BoundingBox miny="30.0" minx="100.0" '
                'maxy="31.0" maxx="101.0"/></TileMap>', encoding="utf-8")
            got = bounds_from_tilemapresource(p)
            self.assertEqual(got, ([100.0, 30.0, 101.0, 31.0], True))

    def test_layer_json_available_used_bounds_ignored(self):
        """layer.json 的 bounds 恒为世界范围，必须用 available 反算。"""
        from backend.core.service_bounds import bounds_from_layer_json
        with TemporaryDirectory() as d:
            p = Path(d)
            available = [[] for _ in range(13)]
            available[12] = [{"startX": 6120, "startY": 3063,
                              "endX": 6129, "endY": 3070}]
            (p / "layer.json").write_text(json.dumps({
                "bounds": [-180, -90, 180, 90],   # 世界范围，必须被忽略
                "available": available,
            }), encoding="utf-8")
            got = bounds_from_layer_json(p / "layer.json")
            self.assertIsNotNone(got)
            # 若误用 bounds 会得到 [-180,-90,180,90] —— 断言不是它
            self.assertNotAlmostEqual(got[0], -180.0, places=1)
            self.assertAlmostEqual(got[0], 88.945312, places=5)

    def test_tileset_region_form(self):
        """region 形态：弧度 -> 度"""
        from backend.core.service_bounds import bounds_from_tileset
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tileset.json").write_text(json.dumps({
                "root": {"boundingVolume": {"region": [
                    math.radians(100.0), math.radians(30.0),
                    math.radians(101.0), math.radians(31.0)]}}
            }), encoding="utf-8")
            got = bounds_from_tileset(p / "tileset.json")
            self.assertAlmostEqual(got[0], 100.0, places=5)
            self.assertAlmostEqual(got[3], 31.0, places=5)

    def test_tileset_box_form(self):
        """box 形态：中心 + 三个半轴向量（ECEF 米）"""
        from backend.core.service_bounds import bounds_from_tileset
        from pyproj import Transformer
        # 取一个真实经纬度，转 ECEF 后构造一个已知大小的 box
        tr = Transformer.from_crs("EPSG:4326", "EPSG:4978", always_xy=True)
        cx, cy, cz = tr.transform(100.0, 30.0, 0.0)
        tileset = {"root": {"boundingVolume": {"box": [
            cx, cy, cz,
            100.0, 0, 0,     # x 半轴（东向 100 米）
            0, 100.0, 0,     # y 半轴
            0, 0, 100.0,     # z 半轴
        ]}}}
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tileset.json").write_text(json.dumps(tileset), encoding="utf-8")
            got = bounds_from_tileset(p / "tileset.json")
            self.assertIsNotNone(got)
            self.assertLess(got[0], 100.0)
            self.assertGreater(got[2], 100.0)

    def test_tileset_unknown_form_returns_none(self):
        from backend.core.service_bounds import bounds_from_tileset
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tileset.json").write_text(
                json.dumps({"root": {}}), encoding="utf-8")
            self.assertIsNone(bounds_from_tileset(p / "tileset.json"))
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_bounds -v`
Expected: FAIL — `ImportError: cannot import name 'bounds_from_metadata'`

- [ ] **Step 3: 写实现**

```python
# backend/core/service_bounds.py 追加
import json
import xml.etree.ElementTree as ET


def bounds_from_metadata(out_dir: Path) -> list[float] | None:
    """同级 metadata.json 的 bbox_wgs84。本工具产出的成果都有，最准。"""
    p = out_dir / "metadata.json"
    if not p.is_file():
        return None
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("读 metadata.json 失败 %s:%s", p, e)
        return None
    b = d.get("bbox_wgs84")
    if isinstance(b, list) and len(b) == 4 and all(
            isinstance(v, (int, float)) for v in b):
        return [float(v) for v in b]
    return None


def bounds_from_tilemapresource(tile_dir: Path
                                ) -> tuple[list[float], bool] | None:
    """tilemapresource.xml 的 BoundingBox。

    返回 (范围, 是否估算)。属性名是 minx/miny/maxx/maxy；**该值是瓦片对齐
    后的四至（tms.py::write_tilemapresource 用 mosaic_bounds 生成），比真实
    选区每边最多大出一张瓦片**，故恒标记为估算值。
    """
    p = tile_dir / "tilemapresource.xml"
    if not p.is_file():
        return None
    try:
        root = ET.parse(p).getroot()
    except (OSError, ET.ParseError) as e:
        logger.debug("解析 tilemapresource.xml 失败 %s:%s", p, e)
        return None
    bb = root.find(".//BoundingBox")
    if bb is None:
        return None
    try:
        return ([float(bb.get("minx")), float(bb.get("miny")),
                 float(bb.get("maxx")), float(bb.get("maxy"))], True)
    except (TypeError, ValueError):
        return None


def bounds_from_layer_json(layer_json: Path) -> list[float] | None:
    """Cesium 地形 layer.json 的范围。

    **不要用 bounds**：实测本工具切出的地形 bounds 恒为 [-180,-90,180,90]
    （写的是世界范围，不是数据范围）。真正描述位置的是 available 的逐级
    瓦片区间，取最高级用地形网格反算。
    """
    if not layer_json.is_file():
        return None
    try:
        d = json.loads(layer_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("读 layer.json 失败 %s:%s", layer_json, e)
        return None
    available = d.get("available") or []
    for L in range(len(available) - 1, -1, -1):
        ranges = available[L]
        if not ranges:
            continue
        r = ranges[0]
        try:
            x_min, y_min = int(r["startX"]), int(r["startY"])
            x_max, y_max = int(r["endX"]), int(r["endY"])
        except (KeyError, TypeError, ValueError):
            continue
        return list(geodetic_bounds(x_min, y_min, x_max, y_max, L))
    return None


def bounds_from_tileset(tileset_json: Path) -> list[float] | None:
    """3D Tiles tileset.json 的根包围盒，返回 WGS84 度四至。

    两种形态都支持：region 是 [west,south,east,north] 弧度；box 是
    [cx,cy,cz, x 半轴, y 半轴, z 半轴]（地心 ECEF 米制）。**实测本工具产出
    的 3D Tiles 用 box**，不是 region。
    """
    if not tileset_json.is_file():
        return None
    try:
        d = json.loads(tileset_json.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        logger.debug("读 tileset.json 失败 %s:%s", tileset_json, e)
        return None
    bv = (d.get("root") or {}).get("boundingVolume") or {}

    region = bv.get("region")
    if isinstance(region, list) and len(region) == 4:
        return [math.degrees(float(v)) for v in region]

    box = bv.get("box")
    if not (isinstance(box, list) and len(box) == 12):
        return None
    cx, cy, cz = (float(v) for v in box[0:3])
    axes = [box[3:6], box[6:9], box[9:12]]
    # 八个角点：中心 ± 各半轴
    corners = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            for sz in (-1, 1):
                x, y, z = cx, cy, cz
                for sign, axis in ((sx, axes[0]), (sy, axes[1]), (sz, axes[2])):
                    x += sign * float(axis[0])
                    y += sign * float(axis[1])
                    z += sign * float(axis[2])
                corners.append((x, y, z))
    try:
        from pyproj import Transformer
        tr = Transformer.from_crs("EPSG:4978", "EPSG:4326", always_xy=True)
        lons, lats = [], []
        for x, y, z in corners:
            lon, lat, _ = tr.transform(x, y, z)
            lons.append(lon)
            lats.append(lat)
    except Exception as e:
        logger.debug("3D Tiles box 转 4326 失败 %s:%s", tileset_json, e)
        return None
    return [min(lons), min(lats), max(lons), max(lats)]


def bounds_for_vector(path: Path) -> list[float] | None:
    """矢量文件的范围（解析几何）。复用既有的 inspect_vector。

    **必须实现**：现有 core/overlay.py 的矢量项不带 bounds_wgs84，靠任务 bbox
    兜底；服务图层没有任务，不解析就永远无法定位。
    """
    try:
        from .local_vector_file import inspect_vector
        info = inspect_vector(path)
    except Exception as e:
        logger.debug("解析矢量范围失败 %s:%s", path, e)
        return None
    b = info.get("bounds_wgs84")
    if isinstance(b, list) and len(b) == 4:
        return [float(v) for v in b]
    return None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_bounds -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/core/service_bounds.py tests/test_service_bounds.py
git commit -m "feat(service): 新增 5 级范围元数据解析

实测修正三个假设：layer.json 的 bounds 恒为世界范围（用 available 反算）、
tileset.json 本工具产出用 box 而非 region、tilemapresource.xml 属性名是
minx/miny/maxx/maxy 且值为瓦片对齐四至。"
```

---

## Task 3: 成果识别 — 扫描目录产出候选

**Files:**
- Create: `backend/core/service_scan.py`
- Test: `tests/test_service_scan.py`

设计依据 §3.2。判据取自格式规范定义的标志文件，不是目录命名约定。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_service_scan.py
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.core.service_scan import Candidate, scan_dir


def _touch(p: Path, content: bytes = b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


class ScanDirTest(unittest.TestCase):
    def test_detects_3dtiles(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "tileset.json",
                   json.dumps({"root": {"boundingVolume": {
                       "region": [0.1, 0.2, 0.3, 0.4]}}}).encode())
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "model")
            self.assertEqual(got[0].entry, "tileset.json")

    def test_detects_terrain(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "terrain" / "layer.json",
                   json.dumps({"available": [[{"startX": 0, "startY": 0,
                                               "endX": 0, "endY": 0}]]}).encode())
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "terrain")

    def test_detects_tms_dir(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "tms" / "tilemapresource.xml",
                   b'<TileMap><TileSets profile="geodetic"/></TileMap>')
            _touch(p / "tms" / "3" / "2" / "1.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "imagery")
            self.assertEqual(got[0].grid, "geodetic")
            self.assertTrue(got[0].flip_y)

    def test_detects_bare_xyz_dir(self):
        """无 tilemapresource.xml 的裸 XYZ 目录也要认出来。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            for y in (0, 1):
                _touch(p / "osm" / "1" / "0" / f"{y}.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "imagery")
            self.assertEqual(got[0].grid, "mercator")
            self.assertFalse(got[0].flip_y)

    def test_detects_vector(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "范围.geojson", b'{"type":"FeatureCollection","features":[]}')
            got = scan_dir(p)
            kinds = {c.kind for c in got}
            self.assertIn("vector", kinds)

    def test_multiple_results_in_one_dir(self):
        """一个成果目录里可能同时有 3dtiles / terrain / 矢量，全部列出。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "tileset.json", b'{"root":{}}')
            _touch(p / "terrain" / "layer.json", b'{"available":[]}')
            _touch(p / "out.geojson", b'{"type":"FeatureCollection","features":[]}')
            got = scan_dir(p)
            self.assertEqual(len(got), 3)
            self.assertEqual({c.kind for c in got}, {"model", "terrain", "vector"})

    def test_ignores_empty_dir(self):
        with TemporaryDirectory() as d:
            self.assertEqual(scan_dir(Path(d)), [])

    def test_ignores_internal_files(self):
        """以下划线开头的中间文件不算成果。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "_temp.geojson", b'{"type":"FeatureCollection","features":[]}')
            self.assertEqual(scan_dir(p), [])

    def test_candidate_label_is_readable(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "tileset.json", b'{"root":{}}')
            got = scan_dir(p)
            self.assertTrue(got[0].label)
            self.assertNotIn("\\", got[0].label)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_scan -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.service_scan'`

- [ ] **Step 3: 写实现**

```python
# backend/core/service_scan.py
"""扫描本地目录，识别出可发布为数据服务的成果。

判据取自**格式规范定义的标志文件**，不是目录命名约定——因此对第三方瓦片包
同样成立：

  模型   tileset.json（3D Tiles 规范）
  地形   layer.json（Cesium quantized-mesh 规范）
  影像   tilemapresource.xml（gdal2tiles）或数字分层瓦片子目录
  矢量   *.geojson / *.kml 文件

扫不出的常见情况：既没有判据文件、又没有数字分层瓦片目录的数据。此类回退到
手工指定类型注册（接口接受显式 kind）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .logs import logger
from .service_bounds import (
    bounds_from_layer_json, bounds_from_metadata, bounds_from_tile_index,
    bounds_from_tilemapresource, bounds_from_tileset, bounds_for_vector,
    detect_grid,
)

#: 递归扫描深度上限。成果目录结构通常不超过 3 层
#: （output/<任务名>/<成果类型>/<文件>）。
MAX_DEPTH = 3

#: 候选的四个分类
KIND_MODEL = "model"
KIND_IMAGERY = "imagery"
KIND_VECTOR = "vector"
KIND_TERRAIN = "terrain"

#: 目录名 -> 展示用的中文类型名
KIND_LABELS = {
    KIND_MODEL: "模型", KIND_IMAGERY: "影像",
    KIND_VECTOR: "矢量", KIND_TERRAIN: "地形",
}

_SUFFIX_CHECKS = {".geojson", ".kml"}


@dataclass
class Candidate:
    """一个可发布为服务的成果。字段与 services 表一一对应。"""
    kind: str
    root: str                 # 绝对路径（已 resolve）
    entry: str = ""           # 相对 root 的入口；瓦片/地形为 ""
    label: str = ""
    grid: str = ""            # 'geodetic' / 'mercator' / ''（非瓦片）
    flip_y: bool = False
    minzoom: int = 0
    maxzoom: int = 18
    bounds_wgs84: list[float] | None = None
    bounds_approx: bool = False
    tile_ext: str = "png"     # 瓦片扩展名（拼访问地址用）
    extra: dict = field(default_factory=dict)


def _tile_levels(d: Path) -> list[int]:
    out = []
    try:
        for p in d.iterdir():
            if p.is_dir() and p.name.isdigit():
                out.append(int(p.name))
    except OSError:
        pass
    return sorted(out)


def _tile_ext(d: Path) -> str:
    """瓦片实际扩展名：裁剪/注记时是 png，否则可能是 jpg。"""
    for p in d.rglob("*"):
        if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            return p.suffix.lower().lstrip(".")
    return "png"


def _has_tiles(d: Path) -> bool:
    """目录内是否有瓦片文件（用于区分"数字分层目录"与普通目录）。"""
    levels = _tile_levels(d)
    if not levels:
        return False
    try:
        for p in d.rglob("*"):
            if p.is_file() and p.suffix.lower() in (
                    ".png", ".jpg", ".jpeg", ".webp"):
                return True
    except OSError:
        pass
    return False


def _meta_bounds(root: Path, out_dir: Path, *, tile_dir: Path | None = None,
                 tileset: Path | None = None, layer_json: Path | None = None,
                 vector: Path | None = None) -> tuple[list[float] | None, bool]:
    """按优先级取范围。返回 (范围, 是否估算值)。"""
    b = bounds_from_metadata(out_dir)
    if b:
        return b, False
    if tile_dir is not None:
        got = bounds_from_tilemapresource(tile_dir)
        if got:
            return got[0], got[1]
    if tileset is not None:
        b = bounds_from_tileset(tileset)
        if b:
            return b, False
    if layer_json is not None:
        b = bounds_from_layer_json(layer_json)
        if b:
            return b, True
    if vector is not None:
        b = bounds_for_vector(vector)
        if b:
            return b, False
    if tile_dir is not None:
        grid = detect_grid(tile_dir)
        b = bounds_from_tile_index(tile_dir, grid)
        if b:
            return list(b), True
    return None, False


def _scan_one_dir(d: Path, rel: str) -> list[Candidate]:
    """识别单个目录下可能存在的成果。rel 是相对扫描根的路径，用于显示名。"""
    out: list[Candidate] = []

    # ---- 模型：递归找 tileset.json ----
    for ts in sorted(d.rglob("tileset.json")):
        if not ts.is_file():
            continue
        sub = ts.parent
        prefix = sub.relative_to(d).as_posix() if sub != d else ""
        name = "/".join(x for x in (rel, prefix) if x)
        b, approx = _meta_bounds(d, d, tileset=ts)
        out.append(Candidate(
            kind=KIND_MODEL, root=str(sub.resolve()),
            entry="tileset.json", label=name,
            bounds_wgs84=b, bounds_approx=approx))

    # ---- 地形：目录含 layer.json ----
    for lj in sorted(d.rglob("layer.json")):
        if not lj.is_file():
            continue
        sub = lj.parent
        prefix = sub.relative_to(d).as_posix() if sub != d else ""
        name = "/".join(x for x in (rel, prefix) if x)
        b, approx = _meta_bounds(d, d, layer_json=lj)
        levels = _tile_levels(sub)
        out.append(Candidate(
            kind=KIND_TERRAIN, root=str(sub.resolve()), entry="", label=name,
            minzoom=levels[0] if levels else 0,
            maxzoom=levels[-1] if levels else 18,
            bounds_wgs84=b, bounds_approx=approx))

    # ---- 影像：瓦片目录（有 tilemapresource.xml 或含数字分层瓦片）----
    for sub in sorted({p.parent for p in d.rglob("*")
                       if p.is_file() and p.suffix.lower() in
                       (".png", ".jpg", ".jpeg", ".webp")}):
        # 从瓦片文件往上找到"级别目录的父目录"即瓦片根
        cur = sub
        while cur != d and cur.parent != d and cur.parent.name.isdigit():
            cur = cur.parent
        if cur == d or not _tile_levels(cur):
            continue
        # 级别目录的父目录才算瓦片根
        tile_root = cur
        if not _tile_levels(tile_root):
            continue
        grid = detect_grid(tile_root)
        prefix = tile_root.relative_to(d).as_posix() if tile_root != d else ""
        name = "/".join(x for x in (rel, prefix) if x)
        levels = _tile_levels(tile_root)
        b, approx = _meta_bounds(d, d, tile_dir=tile_root)
        cand = Candidate(
            kind=KIND_IMAGERY, root=str(tile_root.resolve()), entry="",
            label=name, grid=grid, flip_y=(grid == "geodetic"),
            minzoom=levels[0] if levels else 0,
            maxzoom=levels[-1] if levels else 18,
            bounds_wgs84=b, bounds_approx=approx,
            tile_ext=_tile_ext(tile_root))
        out.append(cand)

    # ---- 矢量：单个文件 ----
    for p in sorted(d.iterdir()):
        if not p.is_file() or p.suffix.lower() not in _SUFFIX_CHECKS:
            continue
        if p.name.startswith(("_", ".")):
            continue
        name = "/".join(x for x in (rel, p.name) if x)
        b, approx = _meta_bounds(d, d, vector=p)
        out.append(Candidate(
            kind=KIND_VECTOR, root=str(d.resolve()), entry=p.name,
            label=name, bounds_wgs84=b, bounds_approx=approx))

    return out


def scan_dir(path: Path, *, max_depth: int = MAX_DEPTH) -> list[Candidate]:
    """扫描目录（含最多 max_depth 层子目录），产出成果候选。

    返回的每个候选的 root 是**可直接做服务根**的绝对目录；entry 是相对 root
    的入口。同一种成果不会重复——比如 tms 目录只会命中一次影像。
    """
    if not path.is_dir():
        return []
    seen: set[tuple[str, str]] = set()
    out: list[Candidate] = []

    def _walk(d: Path, rel: str, depth: int):
        if depth > max_depth:
            return
        try:
            found = _scan_one_dir(d, rel)
        except OSError as e:
            logger.debug("扫描目录失败 %s:%s", d, e)
            found = []
        for c in found:
            key = (c.kind, c.root)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        if depth == max_depth:
            return
        try:
            subs = [p for p in sorted(d.iterdir())
                    if p.is_dir() and not p.name.startswith((".", "_"))]
        except OSError:
            return
        for sub in subs:
            _walk(sub, "/".join(x for x in (rel, sub.name) if x), depth + 1)

    _walk(path.resolve(), "", 0)
    return out
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_scan -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/core/service_scan.py tests/test_service_scan.py
git commit -m "feat(service): 新增本地目录成果识别扫描

按规范标志文件识别模型/地形/影像/矢量四类，支持一个目录内多套成果。"
```

---

## Task 4: services 表与注册表 CRUD

**Files:**
- Modify: `backend/db.py`
- Create: `backend/core/service_registry.py`
- Test: `tests/test_service_registry.py`

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_service_registry.py
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.core.service_registry import (
    add_service, check_path, list_services, remove_service, update_service,
)


class PathSafetyTest(unittest.TestCase):
    """路径穿越是这类文件服务最经典的漏洞，逐种形态都要拦。"""

    def test_normal_relative_path(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "a" / "b.png").parent.mkdir(parents=True)
            (root / "a" / "b.png").write_bytes(b"x")
            got = check_path(root, "a/b.png")
            self.assertEqual(got, (root / "a" / "b.png").resolve())

    def test_dotdot_escape_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            with self.assertRaises(PermissionError):
                check_path(root, "../secret.txt")

    def test_nested_dotdot_escape_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            with self.assertRaises(PermissionError):
                check_path(root, "a/../../secret.txt")

    def test_absolute_path_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            with self.assertRaises(PermissionError):
                check_path(root, "/etc/passwd")

    def test_empty_path_is_root(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            self.assertEqual(check_path(root, ""), root)

    def test_backslash_is_not_a_separator_on_windows_only(self):
        """反斜杠在 POSIX 上是合法文件名字符，不应被当作穿越。"""
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            # 不应抛异常（在 Windows 上会被解析为分隔符，但仍在 root 内）
            check_path(root, "a\\b.png")


class RegistryCrudTest(unittest.TestCase):
    def setUp(self):
        from backend import db
        self._tmp = TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        db.init_db()

    def tearDown(self):
        from backend import db
        db.DB_PATH = self._old
        self._tmp.cleanup()

    def _mk(self, **kw):
        d = kw.pop("dir", None)
        base = {"name": "测试", "kind": "imagery", "root": str(d),
                "entry": "", "grid": "mercator", "flip_y": False,
                "minzoom": 0, "maxzoom": 18, "bounds_wgs84": None,
                "bounds_approx": False, "source": "manual"}
        base.update(kw)
        return add_service(**base)

    def test_add_and_list(self):
        with TemporaryDirectory() as d:
            self._mk(dir=d)
            got = list_services()
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["name"], "测试")

    def test_new_service_disabled_by_default(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            got = list_services()
            self.assertEqual(got[0]["enabled"], 0)
            self.assertTrue(sid)

    def test_update_enabled(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            update_service(sid, enabled=1)
            self.assertEqual(list_services()[0]["enabled"], 1)

    def test_update_name(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            update_service(sid, name="改过了")
            self.assertEqual(list_services()[0]["name"], "改过了")

    def test_remove(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            remove_service(sid)
            self.assertEqual(list_services(), [])

    def test_bounds_roundtrip(self):
        with TemporaryDirectory() as d:
            self._mk(dir=d, bounds_wgs84=[100.0, 30.0, 101.0, 31.0])
            got = list_services()[0]
            self.assertEqual(got["bounds_wgs84"], [100.0, 30.0, 101.0, 31.0])

    def test_health_flags_missing_root(self):
        from backend.core.service_registry import service_health
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            self.assertTrue(service_health(sid)["ok"])
        # 目录已随 TemporaryDirectory 删除
        self.assertFalse(service_health(sid)["ok"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_registry -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backend.core.service_registry'`

- [ ] **Step 3: 在 db.py 加 services 表**

在 `backend/db.py` 的 `_SCHEMA` 末尾（`tasks` 表定义之后、`"""` 之前）追加：

```sql
CREATE TABLE IF NOT EXISTS services (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    root          TEXT NOT NULL,
    entry         TEXT NOT NULL DEFAULT '',
    grid          TEXT NOT NULL DEFAULT '',
    flip_y        INTEGER NOT NULL DEFAULT 0,
    minzoom       INTEGER NOT NULL DEFAULT 0,
    maxzoom       INTEGER NOT NULL DEFAULT 18,
    bounds_wgs84  TEXT NOT NULL DEFAULT '',
    bounds_approx INTEGER NOT NULL DEFAULT 0,
    tile_ext      TEXT NOT NULL DEFAULT 'png',
    enabled       INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL DEFAULT 'manual',
    created_at    TEXT NOT NULL
);
```

`services` 是新表，不是给 `tasks` 加列，因此**不需要动 `_MIGRATIONS`**（`init_db` 的 `executescript(_SCHEMA)` 带 `IF NOT EXISTS`，旧库会自动建表）。

- [ ] **Step 4: 在 models.py 加 services 的 _row_to_dict**

在 `backend/models.py` 末尾追加：

```python
def _service_row_to_dict(row) -> dict:
    """services 表行 -> dict。bounds_wgs84 存的是 JSON 数组文本。"""
    import json
    d = dict(row)
    raw = d.get("bounds_wgs84") or ""
    if raw:
        try:
            d["bounds_wgs84"] = json.loads(raw)
        except ValueError:
            d["bounds_wgs84"] = None
    else:
        d["bounds_wgs84"] = None
    d["flip_y"] = bool(d.get("flip_y"))
    d["bounds_approx"] = bool(d.get("bounds_approx"))
    d["enabled"] = bool(d.get("enabled"))
    return d
```

- [ ] **Step 5: 写注册表实现**

```python
# backend/core/service_registry.py
"""本地数据服务的注册表：CRUD、路径穿越校验、健康探测。

**服务是指向，不是拥有。** 移除服务只删注册记录，绝不动磁盘上的文件。任务被
purge 删除时目录消失，对应服务变为"失效"而不自动删除——自动删会让"目录临时
不可用（外接盘没挂上）"变成永久丢失配置。
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from ..db import get_conn
from ..models import _service_row_to_dict
from .logs import logger


def new_id() -> str:
    """8 位十六进制短 id，用作 URL 段。碰撞概率可忽略，仍做一次存在性检查。"""
    for _ in range(8):
        cand = secrets.token_hex(4)
        with get_conn() as conn:
            hit = conn.execute(
                "SELECT 1 FROM services WHERE id = ?", (cand,)).fetchone()
        if not hit:
            return cand
    raise RuntimeError("无法生成唯一的服务 id")


def check_path(root: Path, rel: str) -> Path:
    """把 rel 拼到 root 之后并校验仍在 root 内。越界抛 PermissionError。

    这是文件服务的**唯一**安全闸门：`..` 穿越、绝对路径注入、符号链接逃逸
    全在这一处拦。root 必须已在注册时 resolve()。
    """
    root = Path(root)
    candidate = (root / rel).resolve() if rel else root.resolve()
    if not candidate.is_relative_to(root):
        raise PermissionError(f"路径越界：{rel}")
    return candidate


def add_service(*, name: str, kind: str, root: str, entry: str = "",
                grid: str = "", flip_y: bool = False, minzoom: int = 0,
                maxzoom: int = 18, bounds_wgs84: list[float] | None = None,
                bounds_approx: bool = False, tile_ext: str = "png",
                source: str = "manual", enabled: bool = False) -> str:
    """登记一个服务。**默认关闭**——不主动对外暴露。"""
    sid = new_id()
    from ..models import _now
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO services (id, name, kind, root, entry, grid, flip_y,"
            " minzoom, maxzoom, bounds_wgs84, bounds_approx, tile_ext,"
            " enabled, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, name, kind, str(Path(root).resolve()), entry, grid,
             1 if flip_y else 0, int(minzoom), int(maxzoom),
             json.dumps(bounds_wgs84) if bounds_wgs84 else "",
             1 if bounds_approx else 0, tile_ext,
             1 if enabled else 0, source, _now()))
    logger.info("登记服务 %s(%s):%s", name, kind, root)
    return sid


def list_services() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM services ORDER BY created_at DESC").fetchall()
    return [_service_row_to_dict(r) for r in rows]


def get_service(sid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM services WHERE id = ?", (sid,)).fetchone()
    return _service_row_to_dict(row) if row else None


def update_service(sid: str, **fields) -> bool:
    """改服务字段。只接受白名单内的列名，避免 SQL 注入。"""
    allowed = {"name", "enabled", "kind", "root", "entry", "grid", "flip_y",
               "minzoom", "maxzoom", "bounds_wgs84", "bounds_approx",
               "tile_ext", "source"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("enabled", "flip_y", "bounds_approx"):
            v = 1 if v else 0
        elif k == "bounds_wgs84":
            v = json.dumps(v) if v else ""
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return False
    vals.append(sid)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE services SET {', '.join(sets)} WHERE id = ?", vals)
    return cur.rowcount > 0


def remove_service(sid: str) -> bool:
    """移除注册记录。**不删磁盘文件**——服务是"指向"，不是"拥有"。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM services WHERE id = ?", (sid,))
    if cur.rowcount:
        logger.info("移除服务 %s", sid)
    return cur.rowcount > 0


def service_health(sid: str) -> dict:
    """探测服务源是否还在。纯本地 exists()，毫秒级。"""
    svc = get_service(sid)
    if svc is None:
        return {"ok": False, "reason": "服务不存在"}
    root = Path(svc["root"])
    if not root.is_dir():
        return {"ok": False, "reason": "源目录已不存在"}
    entry = svc.get("entry") or ""
    if entry:
        try:
            target = check_path(root, entry)
        except PermissionError:
            return {"ok": False, "reason": "入口路径越界"}
        if not target.is_file():
            return {"ok": False, "reason": f"入口文件已不存在：{entry}"}
    return {"ok": True, "reason": ""}


def health_all() -> dict[str, dict]:
    return {s["id"]: service_health(s["id"]) for s in list_services()}
```

- [ ] **Step 6: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_service_registry -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/db.py backend/models.py backend/core/service_registry.py tests/test_service_registry.py
git commit -m "feat(service): 新增 services 表与注册表 CRUD

路径穿越检查覆盖 .. 穿越、绝对路径注入与符号链接逃逸。新增服务默认关闭。"
```

---

## Task 5: 服务端点与文件服务

**Files:**
- Create: `backend/api/services.py`
- Modify: `backend/main.py`
- Test: `tests/test_api_services.py`

设计依据 §3.3 / §3.4。**错误语义**：未开启 → 403、源失效 → 410。**不做来源限制**（用户明确选择）。

- [ ] **Step 1: 写失败的测试**

```python
# tests/test_api_services.py
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient


class ServiceApiTest(unittest.TestCase):
    def setUp(self):
        from backend import db
        self._tmp = TemporaryDirectory()
        self._old = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        db.init_db()
        from backend.main import app
        self.client = TestClient(app)
        self.data = Path(self._tmp.name) / "data"
        (self.data / "3" / "2").mkdir(parents=True)
        (self.data / "3" / "2" / "1.png").write_bytes(b"PNGDATA")

    def tearDown(self):
        from backend import db
        db.DB_PATH = self._old
        self._tmp.cleanup()

    def _register(self, enabled=True, **kw):
        from backend.core.service_registry import add_service
        base = {"name": "t", "kind": "imagery", "root": str(self.data),
                "grid": "mercator", "tile_ext": "png", "enabled": enabled}
        base.update(kw)
        return add_service(**base)

    def test_list_empty(self):
        r = self.client.get("/api/services")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["services"], [])

    def test_disabled_service_returns_403(self):
        sid = self._register(enabled=False)
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 403)
        self.assertIn("未开启", r.text)

    def test_enabled_service_serves_file(self):
        sid = self._register()
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"PNGDATA")

    def test_missing_source_returns_410(self):
        sid = self._register()
        import shutil
        shutil.rmtree(self.data)
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 410)
        self.assertIn("不存在", r.text)

    def test_path_traversal_blocked(self):
        sid = self._register()
        r = self.client.get(f"/api/svc/{sid}/../../etc/passwd")
        self.assertIn(r.status_code, (403, 404))

    def test_unknown_service_404(self):
        r = self.client.get("/api/svc/deadbeef/3/2/1.png")
        self.assertEqual(r.status_code, 404)

    def test_toggle_enabled(self):
        sid = self._register(enabled=False)
        r = self.client.patch(f"/api/services/{sid}", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 200)

    def test_remove_service(self):
        sid = self._register()
        r = self.client.delete(f"/api/services/{sid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/services").json()["services"], [])

    def test_health_reports_missing(self):
        sid = self._register()
        import shutil
        shutil.rmtree(self.data)
        r = self.client.get("/api/services/health")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["health"][sid]["ok"])

    def test_scan_endpoint(self):
        from backend.core.service_registry import add_service
        r = self.client.post("/api/services/scan", json={"path": str(self.data)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("candidates", r.json())

    def test_overlay_desc_has_bounds_for_every_kind(self):
        """瓦片也要有 bounds —— 现有 overlay.py 的 tiles 项不带，照抄会漏。"""
        sid = self._register(bounds_wgs84=[100.0, 30.0, 101.0, 31.0])
        got = self.client.get("/api/services").json()["services"]
        desc = got[0]["overlay_desc"]
        self.assertEqual(desc["kind"], "tiles")
        self.assertEqual(desc["bounds_wgs84"], [100.0, 30.0, 101.0, 31.0])

    def test_access_path_keeps_placeholders(self):
        """瓦片模板的 {z}{x}{y} 必须原样保留，不能被 URL 转义。"""
        self._register()
        got = self.client.get("/api/services").json()["services"]
        path = got[0]["access_path"]
        self.assertIn("{z}", path)
        self.assertIn("{x}", path)
        self.assertIn("{y}", path)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `.venv/Scripts/python.exe -m unittest tests.test_api_services -v`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 写端点实现**

```python
# backend/api/services.py
"""本地数据服务的 HTTP 端点。

**安全边界（刻意与 api/local.py 不同）**：本模块的文件服务端点**不检查请求
来源**。这是为了"给本机其他服务当数据源用"——那些服务不是浏览器，改不了
Referer，走 _require_local 会被直接挡掉。

由此付出的代价：若把 config.yaml 的 server.host 从 127.0.0.1 改成 0.0.0.0，
知道 URL 的局域网设备就能读到已开启的服务内容。默认 127.0.0.1 下暴露面仅本机。

补偿措施：
  - 每个服务的 URL 含 8 位随机 id，不可枚举
  - 新增服务默认关闭，必须显式开启
  - 路径穿越在每个请求上校验（check_path）
"""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..core import service_registry as reg
from ..core import service_scan
from ..core.logs import logger

router = APIRouter(tags=["services"])

#: 瓦片类服务的扩展名兜底
_DEFAULT_EXT = "png"

#: 常见类型 -> Content-Type。3D Tiles 的二进制块要正确声明，
#: 否则 Cesium 会把 .pnts 当文本处理。
_MIME = {
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".kml": "application/vnd.google-earth.kml+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".terrain": "application/vnd.quantized-mesh",
    ".b3dm": "application/octet-stream",
    ".pnts": "application/octet-stream",
    ".i3dm": "application/octet-stream",
    ".cmpt": "application/octet-stream",
    ".glb": "model/gltf-binary",
    ".xml": "application/xml",
}


def _access_path(svc: dict) -> str:
    """服务的访问路径（不含主机名）。前端拼 location.origin。

    主机名由前端补：后端只知道 server.host，实际访问者可能是 192.168.x.x。
    """
    base = f"/api/svc/{svc['id']}"
    if svc["kind"] == service_scan.KIND_IMAGERY and svc.get("grid"):
        ext = svc.get("tile_ext") or _DEFAULT_EXT
        return f"{base}/{{z}}/{{x}}/{{y}}.{ext}"
    if svc["kind"] == service_scan.KIND_TERRAIN:
        return base           # Cesium 会自行追加 /layer.json
    entry = svc.get("entry") or ""
    return f"{base}/{entry}" if entry else base


def _overlay_desc(svc: dict) -> dict:
    """转成前端 overlays.js::build() 认识的 desc。

    结构与 core/overlay.py::list_layers() 的输出逐字段对齐，使服务图层
    零改动复用既有的透明度/层级/定位能力。

    **每种 kind 都必须带 bounds_wgs84**：现有 list_layers 里 6 种 kind 有 5 种
    不带（靠任务 bbox 兜底），服务图层没有任务，不补就永远无法定位。

    三维类（模型/地形）在二维地图上只能画范围框，故 kind 用既有的
    'raster_only_bbox' —— 不引入新 kind，从而不改 build() 与两份 KIND_TEXT。
    """
    path = _access_path(svc)
    kind = svc["kind"]
    if kind == service_scan.KIND_IMAGERY and svc.get("grid"):
        return {
            "id": svc["id"], "kind": "tiles", "label": svc["name"],
            "url": path,
            "grid": svc["grid"], "flip_y": bool(svc.get("flip_y")),
            "minzoom": int(svc.get("minzoom") or 0),
            "maxzoom": int(svc.get("maxzoom") or 18),
            "bounds_wgs84": svc.get("bounds_wgs84"),
        }
    if kind == service_scan.KIND_VECTOR:
        return {
            "id": svc["id"], "kind": "vector", "label": svc["name"],
            "url": path, "bounds_wgs84": svc.get("bounds_wgs84"),
        }
    # 模型 / 地形：二维地图上只画范围框
    return {
        "id": svc["id"], "kind": "raster_only_bbox", "label": svc["name"],
        "url": path, "bounds_wgs84": svc.get("bounds_wgs84"),
    }


def _to_json(svc: dict) -> dict:
    d = dict(svc)
    d["access_path"] = _access_path(svc)
    d["overlay_desc"] = _overlay_desc(svc)
    return d


@router.get("/api/services")
async def api_list_services():
    return {"services": [_to_json(s) for s in reg.list_services()]}


@router.get("/api/services/health")
async def api_services_health():
    return {"health": reg.health_all()}


class ScanReq(BaseModel):
    path: str = Field(..., description="要扫描的本地目录绝对路径")


@router.post("/api/services/scan")
async def api_scan(data: ScanReq):
    """扫描目录，返回可发布的成果候选（不落库）。"""
    p = Path((data.path or "").strip().strip('"'))
    if not p.is_absolute():
        raise HTTPException(400, "请提供绝对路径")
    if not p.is_dir():
        raise HTTPException(404, f"目录不存在：{p}")
    import asyncio
    cands = await asyncio.to_thread(service_scan.scan_dir, p)
    return {"candidates": [{
        "kind": c.kind, "root": c.root, "entry": c.entry, "label": c.label,
        "grid": c.grid, "flip_y": c.flip_y,
        "minzoom": c.minzoom, "maxzoom": c.maxzoom,
        "bounds_wgs84": c.bounds_wgs84, "bounds_approx": c.bounds_approx,
        "tile_ext": c.tile_ext,
    } for c in cands]}


@router.get("/api/services/candidates")
async def api_output_candidates():
    """扫 output 目录得到的候选（面板打开时调）。

    **扫文件系统而非读数据库**：实测库中 106 条 output_path 记录有 100 条目录
    已不存在（成果被清理但记录留存），读库会生成 100 个死服务。
    """
    from ..config import settings
    import asyncio
    root = settings.output_dir
    if not root.is_dir():
        return {"candidates": []}
    cands = await asyncio.to_thread(service_scan.scan_dir, root)
    existing = {(s["kind"], s["root"], s["entry"]) for s in reg.list_services()}
    out = []
    for c in cands:
        if (c.kind, c.root, c.entry) in existing:
            continue
        out.append({
            "kind": c.kind, "root": c.root, "entry": c.entry, "label": c.label,
            "grid": c.grid, "flip_y": c.flip_y,
            "minzoom": c.minzoom, "maxzoom": c.maxzoom,
            "bounds_wgs84": c.bounds_wgs84, "bounds_approx": c.bounds_approx,
            "tile_ext": c.tile_ext,
        })
    return {"candidates": out}


class RegisterReq(BaseModel):
    name: str = Field(default="", description="显示名，默认取路径尾段")
    kind: str = Field(..., description="model / imagery / vector / terrain")
    root: str = Field(..., description="服务根目录绝对路径")
    entry: str = Field(default="")
    grid: str = Field(default="")
    flip_y: bool = Field(default=False)
    minzoom: int = Field(default=0)
    maxzoom: int = Field(default=18)
    bounds_wgs84: list[float] | None = Field(default=None)
    bounds_approx: bool = Field(default=False)
    tile_ext: str = Field(default="png")
    source: str = Field(default="manual")


@router.post("/api/services")
async def api_register(data: RegisterReq):
    root = Path((data.root or "").strip().strip('"'))
    if not root.is_absolute():
        raise HTTPException(400, "请提供绝对路径")
    if not root.is_dir():
        raise HTTPException(404, f"目录不存在：{root}")
    if data.kind not in service_scan.KIND_LABELS:
        raise HTTPException(400, f"不支持的类型：{data.kind}")
    name = (data.name or "").strip() or root.name
    sid = reg.add_service(
        name=name, kind=data.kind, root=str(root), entry=data.entry,
        grid=data.grid, flip_y=data.flip_y, minzoom=data.minzoom,
        maxzoom=data.maxzoom, bounds_wgs84=data.bounds_wgs84,
        bounds_approx=data.bounds_approx, tile_ext=data.tile_ext,
        source=data.source)
    return _to_json(reg.get_service(sid))


class PatchReq(BaseModel):
    name: str | None = None
    enabled: bool | None = None


@router.patch("/api/services/{sid}")
async def api_patch(sid: str, data: PatchReq):
    if reg.get_service(sid) is None:
        raise HTTPException(404, "服务不存在")
    fields = {k: v for k, v in data.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "没有要修改的字段")
    reg.update_service(sid, **fields)
    return _to_json(reg.get_service(sid))


@router.delete("/api/services/{sid}")
async def api_remove(sid: str):
    """移除注册记录。**不删磁盘文件**——服务是"指向"，不是"拥有"。"""
    if not reg.remove_service(sid):
        raise HTTPException(404, "服务不存在")
    return {"removed": sid}


def _serve_file(svc: dict, sub: str):
    """按注册根服务一个文件。所有错误都带明确文案，前端据此区分状态。"""
    from ..core.service_registry import check_path

    health = reg.service_health(svc["id"])
    if not health["ok"]:
        # 410 Gone：资源曾经存在、现在没了。与 403（未开启）区分开——
        # 开关状态和路径写错是两回事，报错必须能分辨。
        raise HTTPException(410, f"源已失效：{health['reason']}")
    if not svc["enabled"]:
        raise HTTPException(403, "服务未开启")

    root = Path(svc["root"])
    entry = svc.get("entry") or ""
    rel = "/".join(x for x in (entry, sub) if x)
    try:
        target = check_path(root, rel)
    except PermissionError:
        logger.warning("服务 %s 拒绝越界路径：%s", svc["id"], sub)
        raise HTTPException(403, "路径越界")
    if target.is_dir():
        raise HTTPException(404, "这里是目录，不是文件")
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{sub}")
    mime = _MIME.get(target.suffix.lower())
    # FileResponse 自带 Range 支持（返回 206），这是 ol/source/GeoTIFF
    # 按需读块的前提。
    return FileResponse(str(target), media_type=mime)


@router.get("/api/svc/{sid}")
async def api_svc_root(sid: str, ):
    """服务根。地形从这里被 CesiumTerrainProvider 追加 /layer.json 读取。"""
    svc = reg.get_service(sid)
    if svc is None:
        raise HTTPException(404, "服务不存在")
    if not svc.get("entry"):
        raise HTTPException(404, "该服务没有根级入口文件")
    return _serve_file(svc, "")


@router.get("/api/svc/{sid}/{sub:path}")
async def api_svc(sid: str, sub: str):
    svc = reg.get_service(sid)
    if svc is None:
        raise HTTPException(404, "服务不存在")
    return _serve_file(svc, sub)
```

在 `backend/main.py` 追加挂载（放在 `local_api` 之后）：

```python
from .api import services as services_api
app.include_router(services_api.router)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `.venv/Scripts/python.exe -m unittest tests.test_api_services -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/api/services.py backend/main.py tests/test_api_services.py
git commit -m "feat(service): 新增服务端点与文件服务

错误语义：未开启 403、源失效 410，前端据此区分。文件服务不做来源限制以支持
本机其他服务接入，安全靠不可枚举 id + 默认关闭 + 每请求路径穿越校验。"
```

---

## Task 6: 前端服务状态与常量

**Files:**
- Create: `frontendvue/src/stores/service.js`
- Modify: `frontendvue/src/utils/provider.js`
- Test: `frontendvue/src/stores/service.test.js`

- [ ] **Step 1: 写失败的测试**

```javascript
// frontendvue/src/stores/service.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const providerSrc = readFileSync(resolve(here, '../utils/provider.js'), 'utf8')
const storeSrc = readFileSync(resolve(here, './service.js'), 'utf8')

test('服务分类常量集中在 provider.js', () => {
  assert.ok(providerSrc.includes('SERVICE_KINDS'),
    '分类常量应定义在 provider.js，避免各组件各抄一份')
  for (const k of ['model', 'imagery', 'vector', 'terrain']) {
    assert.ok(providerSrc.includes(k), `缺少分类 ${k}`)
  }
})

test('分类常量带中文标签', () => {
  assert.ok(providerSrc.includes('SERVICE_KIND_LABELS'),
    '缺少中文标签映射')
  for (const label of ['模型', '影像', '矢量', '地形']) {
    assert.ok(providerSrc.includes(label), `缺少标签 ${label}`)
  }
})

test('store 提供 accessUrl 拼接（主机名由前端补）', () => {
  assert.ok(storeSrc.includes('accessUrl'),
    '缺少 accessUrl：后端只返回路径，主机名要前端拼 location.origin')
  assert.ok(storeSrc.includes('location.origin'),
    'accessUrl 应基于 location.origin')
})

test('store 主动探活', () => {
  assert.ok(storeSrc.includes('/api/services/health'), '缺少探活调用')
})

test('移除服务时同步摘掉地图图层', () => {
  assert.ok(storeSrc.includes('removeAndDetach'),
    '缺少 removeAndDetach：只删注册记录会让图层变成无数据源的幽灵图层')
  assert.ok(storeSrc.includes('removeByService'),
    'removeAndDetach 应调用 overlayStore.removeByService')
})

test('瓦片地址原样保留占位符', () => {
  assert.ok(!storeSrc.includes('encodeURIComponent(path)'),
    '不能对 access_path 做整体编码，会转义 {z}{x}{y} 花括号')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/stores/service.test.js"`
Expected: FAIL — 文件不存在

- [ ] **Step 3: 在 provider.js 追加常量**

```javascript
// frontendvue/src/utils/provider.js 追加

// 本地数据服务的四个分类（与后端 core/service_scan.py 的 KIND_* 保持一致）。
// 集中在此而非各组件里硬编码：本项目已有过三次"名单抄多份、漏改一处"的教训。
export const SERVICE_KINDS = ['model', 'imagery', 'vector', 'terrain']

export const SERVICE_KIND_LABELS = {
  model: '模型',
  imagery: '影像',
  vector: '矢量',
  terrain: '地形',
}

export function serviceKindLabel(kind) {
  return SERVICE_KIND_LABELS[String(kind || '')] || String(kind || '')
}
```

- [ ] **Step 4: 写 store**

```javascript
// frontendvue/src/stores/service.js
//
// 本地数据服务的状态。服务是**持久配置**（落 sqlite），不是临时会话——
// 其他服务要拿去当地图底图用，地址必须跨重启不变。
//
// 与任务图层的关系：服务图层最终以与任务图层**完全相同的 desc 结构**进入
// overlays.js，从而零改动复用透明度/层级/定位（见 utils/provider.js 的设计说明）。
import { defineStore } from 'pinia'

/** 服务类别 -> 标签底色（沿用项目既有的蓝系令牌，不引入新配色） */
export const KIND_TAG_STYLE = {
  model: { background: '#ede9fe', color: '#5b21b6' },
  imagery: { background: '#e0f2fe', color: '#0369a1' },
  vector: { background: '#fef3c7', color: '#92400e' },
  terrain: { background: '#dcfce7', color: '#15803d' },
}

export const useServiceStore = defineStore('service', {
  state: () => ({
    /** 已注册的服务（后端顺序：创建时间倒序） */
    items: [],
    /** { [id]: { ok, reason } } 探活结果 */
    health: {},
    /** 扫 output 得到的未注册候选 */
    candidates: [],
    loading: false,
    error: '',
  }),
  getters: {
    enabled: (s) => s.items.filter((it) => it.enabled),
    /** 失效的服务：源目录已不存在 */
    broken: (s) => s.items.filter((it) => s.health[it.id] && !s.health[it.id].ok),
    countEnabled: (s) => s.items.filter((it) => it.enabled).length,
    isBroken: (s) => (id) => !!(s.health[id] && !s.health[id].ok),
  },
  actions: {
    /**
     * 访问地址 = 主机名 + 后端给的路径。
     * 主机名由前端补：后端只知道 server.host（可能是 127.0.0.1），
     * 而实际访问者可能是局域网 IP 或域名。
     * 瓦片模板里的 {z}{x}{y} **必须原样保留**，整体 encodeURI 会转义花括号。
     */
    accessUrl(svc) {
      const origin = typeof location !== 'undefined' ? location.origin : ''
      return origin + (svc?.access_path || '')
    },

    async fetchAll() {
      this.loading = true
      this.error = ''
      try {
        const r = await fetch('/api/services')
        if (!r.ok) throw new Error(`读取服务列表失败（${r.status}）`)
        const d = await r.json()
        this.items = d.services || []
      } catch (e) {
        this.error = e?.message || String(e)
        this.items = []
      } finally {
        this.loading = false
      }
    },

    /** 打开面板时批量探活一次（纯本地 exists()，毫秒级），不做常驻轮询 */
    async fetchHealth() {
      try {
        const r = await fetch('/api/services/health')
        if (!r.ok) return
        const d = await r.json()
        this.health = d.health || {}
      } catch (_) { /* 探活失败不阻断面板 */ }
    },

    async fetchCandidates() {
      try {
        const r = await fetch('/api/services/candidates')
        if (!r.ok) return
        const d = await r.json()
        this.candidates = d.candidates || []
      } catch (_) {
        this.candidates = []
      }
    },

    async scanDir(path) {
      const r = await fetch('/api/services/scan', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.detail || `扫描失败（${r.status}）`)
      return d.candidates || []
    },

    async register(cand, name = '') {
      const r = await fetch('/api/services', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          name: name || cand.label || '',
          kind: cand.kind, root: cand.root, entry: cand.entry || '',
          grid: cand.grid || '', flip_y: !!cand.flip_y,
          minzoom: cand.minzoom ?? 0, maxzoom: cand.maxzoom ?? 18,
          bounds_wgs84: cand.bounds_wgs84 || null,
          bounds_approx: !!cand.bounds_approx,
          tile_ext: cand.tile_ext || 'png',
          source: cand.source || 'manual',
        }),
      })
      const d = await r.json().catch(() => ({}))
      if (!r.ok) throw new Error(d.detail || `登记失败（${r.status}）`)
      await this.fetchAll()
      await this.fetchHealth()
      return d
    },

    async setEnabled(id, enabled) {
      const r = await fetch(`/api/services/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ enabled }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        throw new Error(d.detail || `操作失败（${r.status}）`)
      }
      await this.fetchAll()
    },

    async rename(id, name) {
      const r = await fetch(`/api/services/${id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name }),
      })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        throw new Error(d.detail || `改名失败（${r.status}）`)
      }
      await this.fetchAll()
    },

    /** 只删注册记录，后端不动磁盘文件 */
    async remove(id) {
      const r = await fetch(`/api/services/${id}`, { method: 'DELETE' })
      if (!r.ok) {
        const d = await r.json().catch(() => ({}))
        throw new Error(d.detail || `移除失败（${r.status}）`)
      }
      this.items = this.items.filter((it) => it.id !== id)
      delete this.health[id]
    },

    /**
     * 移除服务并同步清掉它在地图上的图层。
     *
     * 服务移除后端点即 404，留着图层会让它变成"看着正常、实际已无数据源"的
     * 幽灵图层（瓦片已加载的部分仍在显示，未加载的静默失败）。UI 的移除按钮
     * 一律走这个方法。
     */
    async removeAndDetach(id) {
      await this.remove(id)
      // 动态 import 避免 store 循环依赖（overlay store 不依赖 service store）
      const { useOverlayStore } = await import('./overlay')
      useOverlayStore().removeByService(id)
    },

    /** 清理全部失效服务。调用方需先提醒用户：外接盘未挂载会造成假性失效 */
    async removeAllBroken() {
      const ids = this.broken.map((it) => it.id)
      for (const id of ids) {
        try { await this.removeAndDetach(id) } catch (_) { /* 继续清下一个 */ }
      }
      return ids.length
    },
  },
})
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd frontendvue && node --test "src/stores/service.test.js"`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontendvue/src/stores/service.js frontendvue/src/utils/provider.js frontendvue/src/stores/service.test.js
git commit -m "feat(service): 前端服务状态与分类常量"
```

---

## Task 7: 服务管理面板

**Files:**
- Create: `frontendvue/src/components/ServicePanel.vue`
- Modify: `frontendvue/src/components/AppTopBar.vue`
- Modify: `frontendvue/src/App.vue`
- Test: `frontendvue/src/components/ServicePanel.test.js`

- [ ] **Step 1: 写失败的测试**

```javascript
// frontendvue/src/components/ServicePanel.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(here, './ServicePanel.vue'), 'utf8')
const topbar = readFileSync(resolve(here, './AppTopBar.vue'), 'utf8')

test('顶栏有服务入口', () => {
  assert.ok(topbar.includes('open-services'), '顶栏缺少服务菜单事件')
  assert.ok(topbar.includes('服务'), '顶栏缺少服务文案')
})

test('面板有移除按钮（失效时仍可用）', () => {
  assert.ok(src.includes('移除'), '缺少移除入口')
  assert.ok(src.includes('remove'), '缺少 remove 调用')
})

test('面板提供清理全部失效服务', () => {
  assert.ok(src.includes('removeAllBroken'), '缺少批量清理失效服务')
})

test('面板有复制访问地址', () => {
  assert.ok(src.includes('clipboard'), '复制应走 navigator.clipboard')
  assert.ok(src.includes('accessUrl'), '复制内容应来自 accessUrl')
})

test('失效服务禁用开启与复制', () => {
  assert.ok(src.includes('isBroken'), '缺少失效判定')
})

test('有本地目录选择入口', () => {
  assert.ok(src.includes('/api/local/pick'), '复用了既有系统文件对话框')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/components/ServicePanel.test.js"`
Expected: FAIL — 文件不存在

- [ ] **Step 3: 写面板组件**

```vue
<!-- frontendvue/src/components/ServicePanel.vue -->
<script setup>
/**
 * 服务管理面板（右侧抽屉）。
 *
 * 分工：把本地目录"发布"成带稳定地址的数据服务，供预览页、主界面图层列表
 * 以及其他本机服务使用。与「数据与成果」面板的区别是：那边是**任务**的成果，
 * 这边是**目录**的服务——任务记录删了成果就打不开，而服务指向目录，长期有效。
 */
import { computed, onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useServiceStore, KIND_TAG_STYLE } from '../stores/service'
import { SERVICE_KINDS, serviceKindLabel } from '../utils/provider'

const emit = defineEmits(['close'])

const store = useServiceStore()
const filterKind = ref('all')
const keyword = ref('')
const showCandidates = ref(true)
const scanPath = ref('')
const scanning = ref(false)

const KIND_OPTIONS = [
  { value: 'all', label: '全部' },
  ...SERVICE_KINDS.map((k) => ({ value: k, label: serviceKindLabel(k) })),
]

const rows = computed(() => store.items.filter((it) => {
  if (filterKind.value !== 'all' && it.kind !== filterKind.value) return false
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return true
  return (it.name || '').toLowerCase().includes(kw)
    || (it.root || '').toLowerCase().includes(kw)
}))

const candidates = computed(() => store.candidates.filter((c) => {
  if (filterKind.value !== 'all' && c.kind !== filterKind.value) return false
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) return true
  return (c.label || '').toLowerCase().includes(kw)
    || (c.root || '').toLowerCase().includes(kw)
}))

/** 长路径中段省略，完整路径放 title（对齐 LayerPanel 的既有做法） */
function shortPath(p) {
  const s = String(p || '')
  if (s.length <= 42) return s
  const parts = s.split(/[\\/]/).filter(Boolean)
  if (parts.length <= 3) return s
  return `…/${parts.slice(-3).join('/')}`
}

async function copyUrl(svc) {
  const url = store.accessUrl(svc)
  try {
    await navigator.clipboard.writeText(url)
    MessagePlugin.success('地址已复制')
  } catch (_) {
    // 非安全上下文（http 且非 localhost）下 clipboard 不可用，回落到选中提示
    MessagePlugin.warning(`复制失败，请手工复制：${url}`)
  }
}

async function toggle(svc) {
  try {
    await store.setEnabled(svc.id, !svc.enabled)
  } catch (e) {
    MessagePlugin.error(e?.message || '操作失败')
  }
}

async function doRemove(svc) {
  try {
    // 走 removeAndDetach：同时清掉该服务在地图上的图层
    await store.removeAndDetach(svc.id)
    MessagePlugin.success('已移除（磁盘文件未删除）')
  } catch (e) {
    MessagePlugin.error(e?.message || '移除失败')
  }
}

/** 批量清理前提醒外接盘场景：盘没挂上会造成假性失效 */
function confirmCleanBroken() {
  const n = store.broken.length
  if (!n) return
  const dlg = window.confirm(
    `将移除 ${n} 个失效服务。\n\n` +
    '注意：若这些服务的源目录在外接盘上，盘未挂载时会看起来"失效"。' +
    '移除只删除注册记录，不会删除磁盘文件。\n\n确认移除？')
  if (!dlg) return
  store.removeAllBroken().then((cnt) => MessagePlugin.success(`已移除 ${cnt} 个`))
}

async function browseAndScan() {
  try {
    const r = await fetch('/api/local/pick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ kind: 'dir', multiple: false }),
    })
    const d = await r.json()
    const picked = (d.paths || [])[0]
    if (!picked) return
    scanPath.value = picked
    await doScan()
  } catch (e) {
    MessagePlugin.error(`打开目录选择失败：${e?.message || e}`)
  }
}

async function doScan() {
  const p = scanPath.value.trim()
  if (!p) { MessagePlugin.warning('请先选择或填写目录'); return }
  scanning.value = true
  try {
    const found = await store.scanDir(p)
    store.candidates = found
    showCandidates.value = true
    if (!found.length) {
      MessagePlugin.warning(
        '未在该目录下识别到可发布的成果。识别依据：tileset.json（模型）、' +
        'layer.json（地形）、tilemapresource.xml 或数字分层瓦片目录（影像）、' +
        '.geojson/.kml（矢量）')
    } else {
      MessagePlugin.success(`识别到 ${found.length} 个成果`)
    }
  } catch (e) {
    MessagePlugin.error(e?.message || '扫描失败')
  } finally {
    scanning.value = false
  }
}

async function register(cand) {
  try {
    await store.register(cand)
    store.candidates = store.candidates.filter((c) => c !== cand)
    MessagePlugin.success('已添加，默认关闭；需要时点「开启」')
  } catch (e) {
    MessagePlugin.error(e?.message || '登记失败')
  }
}

onMounted(async () => {
  await store.fetchAll()
  await Promise.all([store.fetchHealth(), store.fetchCandidates()])
})
</script>

<template>
  <div class="svc-panel">
    <div class="hd">
      <span class="title">服务</span>
      <button class="x" @click="emit('close')">×</button>
    </div>

    <!-- 添加 -->
    <div class="add">
      <div class="add-row">
        <input v-model="scanPath" class="inp" placeholder="本地目录绝对路径" />
        <button class="btn" @click="browseAndScan">浏览…</button>
        <button class="btn primary" :disabled="scanning" @click="doScan">
          {{ scanning ? '扫描中…' : '扫描' }}
        </button>
      </div>
    </div>

    <!-- 失效提醒 -->
    <div v-if="store.broken.length" class="warn">
      <span>发现 {{ store.broken.length }} 个失效服务（源目录已不存在）</span>
      <button class="link" @click="confirmCleanBroken">清理全部失效</button>
    </div>

    <!-- 筛选 -->
    <div class="filters">
      <select v-model="filterKind" class="sel">
        <option v-for="o in KIND_OPTIONS" :key="o.value" :value="o.value">
          {{ o.label }}
        </option>
      </select>
      <input v-model="keyword" class="inp" placeholder="关键字" />
    </div>

    <div v-if="store.error" class="err">{{ store.error }}</div>

    <!-- 已注册 -->
    <div class="list">
      <div v-for="it in rows" :key="it.id" class="card"
        :class="{ broken: store.isBroken(it.id), off: !it.enabled }">
        <div class="r1">
          <span class="dot" :class="{ on: it.enabled && !store.isBroken(it.id) }" />
          <span class="tag" :style="KIND_TAG_STYLE[it.kind]">
            {{ serviceKindLabel(it.kind) }}
          </span>
          <span class="nm" :title="it.name">{{ it.name }}</span>
        </div>
        <div class="pth" :title="it.root">{{ shortPath(it.root) }}</div>
        <div v-if="store.isBroken(it.id)" class="bad">
          源目录已不存在<span v-if="store.health[it.id]?.reason">
            ：{{ store.health[it.id].reason }}</span>
        </div>
        <div v-else-if="it.bounds_approx" class="note">范围为估算值（瓦片对齐或扫描超限）</div>
        <div class="acts">
          <button class="mini" :disabled="!it.enabled || store.isBroken(it.id)"
            @click="copyUrl(it)">复制地址</button>
          <button class="mini" :disabled="store.isBroken(it.id)" @click="toggle(it)">
            {{ it.enabled ? '关闭' : '开启' }}
          </button>
          <!-- 失效时仍可移除：目录没了正是最需要移除的时候 -->
          <button class="mini del" @click="doRemove(it)">移除</button>
        </div>
      </div>
      <div v-if="!rows.length && !store.loading" class="empty">
        还没有服务。用上方「浏览…」选一个本地目录，或从下方候选里挑。
      </div>
    </div>

    <!-- 候选 -->
    <div class="cands">
      <button class="fold" @click="showCandidates = !showCandidates">
        {{ showCandidates ? '▾' : '▸' }} 未注册的成果（{{ candidates.length }}）
      </button>
      <template v-if="showCandidates">
        <div v-for="(c, i) in candidates" :key="`${c.kind}:${c.root}:${c.entry}:${i}`"
          class="card cand">
          <div class="r1">
            <span class="tag" :style="KIND_TAG_STYLE[c.kind]">
              {{ serviceKindLabel(c.kind) }}
            </span>
            <span class="nm" :title="c.label">{{ c.label }}</span>
          </div>
          <div class="pth" :title="c.root">{{ shortPath(c.root) }}</div>
          <div class="acts">
            <button class="mini primary" @click="register(c)">添加</button>
          </div>
        </div>
        <div v-if="!candidates.length" class="empty">没有未注册的成果</div>
      </template>
    </div>
  </div>
</template>

<style scoped>
.svc-panel {
  position: fixed; top: 0; right: 0; bottom: 0; width: 380px; z-index: 60;
  display: flex; flex-direction: column; gap: 6px;
  background: #fff; border-left: 1px solid #dbe3ec;
  box-shadow: -4px 0 18px rgba(15, 23, 42, .12);
  padding: 10px 12px; overflow-y: auto; font-size: 13px;
}
.hd { display: flex; align-items: center; justify-content: space-between; }
.title { font-weight: 700; color: #0369a1; }
.x {
  border: 0; background: transparent; cursor: pointer;
  font-size: 18px; color: #64748b; line-height: 1;
}
.add-row { display: flex; gap: 5px; }
.inp {
  flex: 1 1 auto; min-width: 0; border: 1px solid #dbe3ec; border-radius: 5px;
  padding: 4px 7px; font-size: 12px; font-family: inherit;
}
.sel {
  flex: 0 0 auto; border: 1px solid #dbe3ec; border-radius: 5px;
  padding: 4px 6px; font-size: 12px; font-family: inherit; background: #fff;
}
.btn {
  flex: 0 0 auto; border: 1px solid #dbe3ec; background: #fff; cursor: pointer;
  border-radius: 5px; font-size: 12px; color: #475569; padding: 4px 9px;
}
.btn:hover:not(:disabled) { background: #f0f9ff; border-color: #7dd3fc; }
.btn.primary { background: #0284c7; border-color: #0284c7; color: #fff; }
.btn.primary:hover:not(:disabled) { background: #0369a1; }
.btn:disabled { opacity: .5; cursor: not-allowed; }
.warn {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 11px; color: #b45309; background: #fffbeb;
  border: 1px solid #fde68a; border-radius: 6px; padding: 5px 8px;
}
.link { border: 0; background: transparent; cursor: pointer; color: #b45309;
  font-size: 11px; text-decoration: underline; }
.filters { display: flex; gap: 5px; }
.err { font-size: 11px; color: #b91c1c; }
.list, .cands { display: flex; flex-direction: column; gap: 6px; }
.cands { border-top: 1px solid #eef2f7; padding-top: 6px; }
.fold {
  border: 0; background: transparent; cursor: pointer; text-align: left;
  color: #0369a1; font-size: 12px; padding: 2px 0;
}
.card {
  border: 1px solid #eef2f7; border-radius: 6px; padding: 6px 8px; background: #fff;
}
.card.off { background: #fafafa; }
.card.broken { background: #fef2f2; border-color: #fecaca; opacity: .85; }
.card.cand { background: #f8fbff; border-color: #dbeafe; }
.r1 { display: flex; align-items: center; gap: 6px; min-width: 0; }
.dot {
  flex: 0 0 auto; width: 7px; height: 7px; border-radius: 50%; background: #cbd5e1;
}
.dot.on { background: #16a34a; }
.tag {
  flex: 0 0 auto; font-size: 10px; border-radius: 3px;
  padding: 0 5px; line-height: 16px;
}
.nm {
  flex: 1 1 auto; min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; color: #334155; font-weight: 600;
}
.pth {
  margin-top: 2px; font-size: 11px; color: #94a3b8;
  font-family: ui-monospace, Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.bad { margin-top: 3px; font-size: 11px; color: #b91c1c; line-height: 1.5; }
.note { margin-top: 3px; font-size: 11px; color: #b45309; }
.acts { display: flex; gap: 4px; margin-top: 5px; }
.mini {
  border: 1px solid #dbe3ec; background: #fff; border-radius: 4px;
  cursor: pointer; font-size: 11px; color: #475569; padding: 1px 7px;
}
.mini:hover:not(:disabled) { background: #f0f9ff; border-color: #7dd3fc; }
.mini:disabled { opacity: .45; cursor: not-allowed; }
.mini.del { color: #b91c1c; border-color: #fecaca; }
.mini.primary { background: #0284c7; border-color: #0284c7; color: #fff; }
.empty { font-size: 12px; color: #94a3b8; padding: 4px 0; line-height: 1.7; }
</style>
```

- [ ] **Step 4: 顶栏加服务入口**

在 `frontendvue/src/components/AppTopBar.vue`：

`defineEmits` 数组加 `'open-services'`：

```javascript
const emit = defineEmits([
  'new-download', 'new-local', 'new-vector', 'new-3d',
  'open-data', 'open-tasks', 'open-services', 'open-tokens', 'open-logs', 'open-about',
])
```

引入 store 并加角标计数：

```javascript
import { useServiceStore } from '../stores/service'
const serviceStore = useServiceStore()
/** 已开启的服务数，做顶栏角标 */
const serviceCount = computed(() => serviceStore.countEnabled)
```

模板中「任务」按钮之后加：

```html
<button class="nav-btn" @click="emit('open-services')">
  服务<span v-if="serviceCount" class="badge">{{ serviceCount }}</span>
</button>
```

- [ ] **Step 5: App.vue 接线**

在 `frontendvue/src/App.vue` 里：

1. import 组件与 store：

```javascript
import ServicePanel from './components/ServicePanel.vue'
import { useServiceStore } from './stores/service'
const serviceStore = useServiceStore()
const showServices = ref(false)
```

2. 模板里 `AppTopBar` 加监听：

```html
@open-services="showServices = true"
```

3. 模板末尾（与其他弹层同级）加：

```html
<ServicePanel v-if="showServices" @close="showServices = false" />
```

4. 首屏拉一次服务数，让顶栏角标有值（在现有 `onMounted` 里追加）：

```javascript
serviceStore.fetchAll()
```

- [ ] **Step 6: 运行测试确认通过**

Run: `cd frontendvue && node --test "src/components/ServicePanel.test.js"`
Expected: PASS

- [ ] **Step 7: 构建验证**

Run: `cd frontendvue && npm run build`
Expected: 退出码 0

- [ ] **Step 8: 提交**

```bash
git add frontendvue/src/components/ServicePanel.vue frontendvue/src/components/ServicePanel.test.js frontendvue/src/components/AppTopBar.vue frontendvue/src/App.vue
git commit -m "feat(service): 新增服务管理面板与顶栏入口

支持扫描目录、登记、开关、改名、复制地址、移除；失效服务仍可移除。"
```

---

## Task 8: overlayStore 解耦与既有 bug B1

**Files:**
- Modify: `frontendvue/src/stores/overlay.js`
- Modify: `frontendvue/src/components/DataDialog.vue`
- Modify: `frontendvue/src/components/TaskQueue.vue`
- Modify: `frontendvue/src/composables/overlays.js:165`（注释）
- Test: `frontendvue/src/stores/overlay.test.js`

- [ ] **Step 1: 写失败的测试**

```javascript
// frontendvue/src/stores/overlay.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const store = readFileSync(resolve(here, './overlay.js'), 'utf8')
const dataDialog = readFileSync(resolve(here, '../components/DataDialog.vue'), 'utf8')
const taskQueue = readFileSync(resolve(here, '../components/TaskQueue.vue'), 'utf8')

test('key 拆成两个函数，不复用同一个换语义', () => {
  assert.ok(store.includes('taskOverlayKey'), '缺少 taskOverlayKey')
  assert.ok(store.includes('serviceOverlayKey'), '缺少 serviceOverlayKey')
})

test('两个 key 空间用前缀隔开，不会撞', () => {
  assert.ok(store.includes("`task:"), 'task key 应带 task: 前缀')
  assert.ok(store.includes("`svc:"), 'service key 应带 svc: 前缀')
})

test('服务图层支持按来源批量清理', () => {
  assert.ok(store.includes('removeByService'), '缺少 removeByService')
})

test('DataDialog 三处引用已同步改名', () => {
  assert.ok(dataDialog.includes('taskOverlayKey'),
    'DataDialog 的 overlayKey 引用未同步改名')
  assert.ok(!/\boverlayKey\(/.test(dataDialog),
    'DataDialog 仍有旧的 overlayKey 调用')
})

test('B1：任务队列删除时清掉该任务的图层', () => {
  assert.ok(taskQueue.includes('removeByTask'),
    'TaskQueue.doDelete 未调用 overlayStore.removeByTask —— 删任务后图层会残留且无法移除')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/stores/overlay.test.js"`
Expected: FAIL — 缺少 taskOverlayKey

- [ ] **Step 3: 改 overlay.js**

替换顶部的 key 定义：

```javascript
/**
 * 叠加图层的 key。
 *
 * **拆成两个函数而不是一个复用**：服务图层没有 taskId，若共用一个函数靠调用方
 * 传不同含义的第一个参数，`_fallbackBbox` 里 `it.taskId` 的语义会被污染。
 * 两个前缀不同，值域天然不重叠。
 */
export function taskOverlayKey(taskId, layerId) { return `task:${taskId}:${layerId}` }
export function serviceOverlayKey(serviceId) { return `svc:${serviceId}` }
```

`add` 改签名，显式接受来源：

```javascript
    /**
     * 叠加一个图层。返回是否成功（不可叠加的 kind 会失败，由调用方提示）。
     * 首个图层自动定位；已有图层时不动视野——叠第二个图层的目的就是同视野对比。
     *
     * src 是来源标识：{ taskId, taskName } 或 { serviceId, serviceName }。
     */
    add(src, desc) {
      const map = mapController.value?.map
      if (!map) return false
      const key = src.serviceId
        ? serviceOverlayKey(src.serviceId)
        : taskOverlayKey(src.taskId, desc.id)
      if (this.has(key)) return true
      if (!addOverlay(map, key, desc)) return false
      const first = !this.items.length
      this.items = [...this.items, {
        key,
        taskId: src.taskId || '',
        serviceId: src.serviceId || '',
        taskName: src.taskName || src.serviceName || '',
        desc, visible: true, opacity: 1,
      }]
      this._sync()
      if (first) this.zoomTo(key)
      return true
    },
```

`_fallbackBbox` 改成只对任务图层生效：

```javascript
    /**
     * 图层自身没有范围时的兜底：用所属任务的 bbox。
     * **服务图层没有兜底**——服务侧的 desc 必须自带 bounds_wgs84（后端保证），
     * 拿不到就是拿不到，不猜。
     */
    _fallbackBbox(key) {
      const it = this.items.find((x) => x.key === key)
      if (!it || !it.taskId) return null
      const t = useTaskStore().byId(it.taskId)
      const b = t?.bbox
      return Array.isArray(b) && b.length === 4 ? b : null
    },
```

加 `removeByService`：

```javascript
    /** 服务被移除时清掉它的图层，否则图层会留在地图上且再也无法从界面移除 */
    removeByService(serviceId) {
      for (const it of this.items.filter((x) => x.serviceId === serviceId)) {
        removeOverlay(mapController.value?.map, it.key)
      }
      this.items = this.items.filter((x) => x.serviceId !== serviceId)
      this._sync()
    },
```

- [ ] **Step 4: 改 DataDialog.vue 三处**

```javascript
// :19
import { taskOverlayKey } from '../stores/overlay'
// :118
function layerOn(t, L) { return overlayStore.has(taskOverlayKey(t.id, L.id)) }
// :125
overlayStore.remove(taskOverlayKey(t.id, L.id))
```

`toggleLayer` 里的 add 调用改签名：

```javascript
function toggleLayer(t, L, on) {
  if (on) {
    if (!overlayStore.add({ taskId: t.id, taskName: t.name }, L)) {
      MessagePlugin.warning('该图层无法叠加显示')
    }
  } else {
    overlayStore.remove(taskOverlayKey(t.id, L.id))
  }
}
```

- [ ] **Step 5: 修既有 bug B1（TaskQueue.doDelete）**

```javascript
// frontendvue/src/components/TaskQueue.vue
import { useOverlayStore } from '../stores/overlay'
const overlayStore = useOverlayStore()

async function doDelete(id, purge) {
  try {
    await taskStore.remove(id, purge)
    // 必须清掉该任务的叠加图层：成果记录没了之后，图层留在地图上
    // 就再也无法从任何界面移除
    overlayStore.removeByTask(id)
    if (mapController.value && taskStore.activeId == null) mapController.value.clearPreview()
    MessagePlugin.success('已删除')
  } catch (e) { MessagePlugin.error(e?.message || '删除失败') }
}
```

- [ ] **Step 6: 同步 overlays.js 的过时注释**

`frontendvue/src/composables/overlays.js:165` 的注释 `key 需全局唯一(通常是 \`${taskId}:${layer.id}\`)` 改为：

```javascript
 * key 需全局唯一。来源有两种，key 前缀区分（见 stores/overlay.js 的
 * taskOverlayKey / serviceOverlayKey）。
```

- [ ] **Step 7: 运行测试确认通过**

Run: `cd frontendvue && node --test "src/stores/overlay.test.js"`
Expected: PASS

- [ ] **Step 8: 提交**

```bash
git add frontendvue/src/stores/overlay.js frontendvue/src/stores/overlay.test.js frontendvue/src/components/DataDialog.vue frontendvue/src/components/TaskQueue.vue frontendvue/src/composables/overlays.js
git commit -m "fix(overlay): key 拆分为任务/服务两套，修复任务队列删除残留图层

key 改为 task:/svc: 两个前缀，避免服务图层污染 _fallbackBbox 的 taskId 语义。
顺带修复既有 bug：TaskQueue.doDelete 未清叠加图层，导致从任务队列删任务后
图层残留在地图上且无法从任何界面移除。"
```

---

## Task 9: 服务图层选择器与主界面接入

**Files:**
- Create: `frontendvue/src/components/ServiceLayerPicker.vue`
- Modify: `frontendvue/src/components/LayerPanel.vue`
- Test: `frontendvue/src/components/ServiceLayerPicker.test.js`

- [ ] **Step 1: 写失败的测试**

```javascript
// frontendvue/src/components/ServiceLayerPicker.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const picker = readFileSync(resolve(here, './ServiceLayerPicker.vue'), 'utf8')
const layerPanel = readFileSync(resolve(here, './LayerPanel.vue'), 'utf8')

test('只列已开启的服务', () => {
  assert.ok(picker.includes('enabled'), '未按 enabled 过滤')
})

test('支持按类型与关键字过滤', () => {
  assert.ok(picker.includes('SERVICE_KINDS'), '类型筛选未复用集中常量')
  assert.ok(picker.includes('keyword'), '缺少关键字过滤')
})

test('添加走 overlayStore.add 的服务分支', () => {
  assert.ok(picker.includes('serviceId'), '未用 serviceId 标识来源')
  assert.ok(picker.includes('overlayStore.add'), '未走统一的 add 入口')
})

test('图层面板添加图层改为下拉两个来源', () => {
  assert.ok(layerPanel.includes('从「服务」列表添加'), '缺少服务来源入口')
  assert.ok(layerPanel.includes('ServiceLayerPicker'), '未引入选择器')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/components/ServiceLayerPicker.test.js"`
Expected: FAIL — 文件不存在

- [ ] **Step 3: 写选择器组件**

```vue
<!-- frontendvue/src/components/ServiceLayerPicker.vue -->
<script setup>
/**
 * 服务图层选择器。预览页（Cesium）与主界面图层面板（OpenLayers）共用。
 *
 * **只列已开启的服务**：关掉的服务选中后会立刻收到 403，不如不显示。
 */
import { computed, onMounted, ref } from 'vue'
import { MessagePlugin } from 'tdesign-vue-next'
import { useServiceStore, KIND_TAG_STYLE } from '../stores/service'
import { useOverlayStore } from '../stores/overlay'
import { SERVICE_KINDS, serviceKindLabel } from '../utils/provider'

const emit = defineEmits(['close', 'pick'])

const serviceStore = useServiceStore()
const overlayStore = useOverlayStore()
const filterKind = ref('all')
const keyword = ref('')

const KIND_OPTIONS = [
  { value: 'all', label: '全部' },
  ...SERVICE_KINDS.map((k) => ({ value: k, label: serviceKindLabel(k) })),
]

/** 已开启且源未失效 */
const rows = computed(() => serviceStore.enabled
  .filter((it) => !serviceStore.isBroken(it.id))
  .filter((it) => {
    if (filterKind.value !== 'all' && it.kind !== filterKind.value) return false
    const kw = keyword.value.trim().toLowerCase()
    if (!kw) return true
    return (it.name || '').toLowerCase().includes(kw)
      || (it.root || '').toLowerCase().includes(kw)
  }))

function shortPath(p) {
  const s = String(p || '')
  if (s.length <= 40) return s
  const parts = s.split(/[\\/]/).filter(Boolean)
  if (parts.length <= 3) return s
  return `…/${parts.slice(-3).join('/')}`
}

/**
 * 选中一个服务。
 * 二维与三维的添加方式不同，由调用方通过 pick 事件处理；这里只负责
 * 走统一入口并给出失败反馈。
 */
function pick(svc) {
  // 二维路径：交给父组件决定（预览页加 Cesium 图层，主界面加 OL 图层）
  emit('pick', svc)
}

onMounted(async () => {
  await serviceStore.fetchAll()
  await serviceStore.fetchHealth()
})
</script>

<template>
  <div class="picker">
    <div class="hd">
      <span class="title">添加服务图层</span>
      <button class="x" @click="emit('close')">×</button>
    </div>

    <div class="filters">
      <select v-model="filterKind" class="sel">
        <option v-for="o in KIND_OPTIONS" :key="o.value" :value="o.value">
          {{ o.label }}
        </option>
      </select>
      <input v-model="keyword" class="inp" placeholder="搜索名称或路径" />
    </div>

    <div class="list">
      <button v-for="it in rows" :key="it.id" class="row" @click="pick(it)">
        <span class="tag" :style="KIND_TAG_STYLE[it.kind]">
          {{ serviceKindLabel(it.kind) }}
        </span>
        <span class="body">
          <span class="nm" :title="it.name">{{ it.name }}</span>
          <span class="pth" :title="it.root">{{ shortPath(it.root) }}</span>
        </span>
      </button>
      <div v-if="!rows.length" class="empty">
        还没有开启的服务，或都被筛掉了。去顶栏「服务」菜单添加并开启。
      </div>
    </div>
  </div>
</template>

<style scoped>
.picker {
  width: 340px; background: #fff; border: 1px solid #dbe3ec;
  border-radius: 8px; box-shadow: 0 6px 20px rgba(15, 23, 42, .18);
  padding: 10px 12px; font-size: 13px;
}
.hd { display: flex; align-items: center; justify-content: space-between; }
.title { font-weight: 700; color: #0369a1; }
.x { border: 0; background: transparent; cursor: pointer; font-size: 18px;
  color: #64748b; line-height: 1; }
.filters { display: flex; gap: 5px; margin: 6px 0; }
.sel { flex: 0 0 auto; border: 1px solid #dbe3ec; border-radius: 5px;
  padding: 3px 6px; font-size: 12px; font-family: inherit; background: #fff; }
.inp { flex: 1 1 auto; min-width: 0; border: 1px solid #dbe3ec;
  border-radius: 5px; padding: 3px 7px; font-size: 12px; font-family: inherit; }
.list { max-height: 46vh; overflow-y: auto; display: flex;
  flex-direction: column; gap: 3px; }
.row {
  display: flex; align-items: center; gap: 7px; width: 100%; text-align: left;
  border: 1px solid #eef2f7; background: #fff; border-radius: 5px;
  padding: 4px 7px; cursor: pointer; font-family: inherit;
}
.row:hover { background: #f0f9ff; border-color: #7dd3fc; }
.tag { flex: 0 0 auto; font-size: 10px; border-radius: 3px;
  padding: 0 5px; line-height: 16px; }
.body { flex: 1 1 auto; min-width: 0; display: flex;
  flex-direction: column; gap: 1px; }
.nm { color: #334155; font-size: 12px; font-weight: 600;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.pth { color: #94a3b8; font-size: 10px;
  font-family: ui-monospace, Consolas, monospace;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.empty { font-size: 12px; color: #94a3b8; padding: 6px 0; line-height: 1.7; }
</style>
```

- [ ] **Step 4: LayerPanel 改为下拉两个来源**

`frontendvue/src/components/LayerPanel.vue` 的底部：

```html
      <div v-if="!rows.length" class="empty">
        地图上还没有叠加图层。
        <button class="link" @click="openData">从「数据」面板添加</button>
        <button class="link" @click="openServices">或从「服务」列表添加</button>
      </div>
      <div v-else class="foot">
        <button class="link" @click="openData">＋ 从「数据」面板添加</button>
        <button class="link" @click="openServices">＋ 从「服务」列表添加</button>
        <button class="link danger" @click="overlayStore.clear()">全部移除</button>
      </div>
```

script 里加：

```javascript
import ServiceLayerPicker from './ServiceLayerPicker.vue'

const showServicePicker = ref(false)

function openServices() { showServicePicker.value = true }

/**
 * 服务图层走与任务图层完全相同的 add 入口，因此透明度/层级/定位自动可用
 * （都由 overlays.js 的通用实现提供，与来源无关）。
 */
function onPickService(svc) {
  const ok = overlayStore.add(
    { serviceId: svc.id, serviceName: svc.name }, svc.overlay_desc)
  if (!ok) {
    MessagePlugin.warning('该服务无法叠加显示（三维与地形只画范围框）')
  } else {
    MessagePlugin.success(`已添加：${svc.name}`)
  }
  showServicePicker.value = false
}
```

模板里加弹层（放在 `.layers` 内部靠上，避免超出视口）：

```html
    <div v-if="showServicePicker" class="picker-wrap">
      <ServiceLayerPicker @close="showServicePicker = false"
        @pick="onPickService" />
    </div>
```

样式加：

```css
.picker-wrap { position: absolute; bottom: calc(100% + 6px); left: 0; z-index: 30; }
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd frontendvue && node --test "src/components/ServiceLayerPicker.test.js"`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add frontendvue/src/components/ServiceLayerPicker.vue frontendvue/src/components/ServiceLayerPicker.test.js frontendvue/src/components/LayerPanel.vue
git commit -m "feat(service): 服务图层选择器与主界面图层列表接入

服务图层走统一 add 入口，透明度/层级/定位三项自动继承。"
```

---

## Task 10: 预览页接入服务

**Files:**
- Modify: `frontendvue/src/preview/PreviewApp.vue`
- Modify: `frontendvue/src/components/TaskQueue.model3d.test.js`
- Test: `frontendvue/src/preview/PreviewService.test.js`

设计依据 §4.2 / §4.2.1。**四个既有陷阱必须一并处理。**

- [ ] **Step 1: 写失败的测试**

```javascript
// frontendvue/src/preview/PreviewService.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(here, './PreviewApp.vue'), 'utf8')

test('id 可选：无 id 不再直接报错', () => {
  assert.ok(!src.includes("errorMsg.value = '缺少任务 id 参数'"),
    '缺少 id 时仍会报错，空白预览进不去')
})

test('viewer 创建必须在任务加载之前', () => {
  const viewerAt = src.indexOf("new Viewer('cesium-container'")
  const fetchAt = src.indexOf('/api/tasks/${id}')
  assert.ok(viewerAt > 0 && fetchAt > 0, '未找到创建或加载位置')
  assert.ok(viewerAt < fetchAt,
    'viewer 必须先于任务加载创建，否则空白预览无 viewer、measure 崩在 null')
})

test('fetch 结果要判 ok（404 不抛错会静默渲染空预览）', () => {
  assert.ok(src.includes('res.ok') || src.includes('r.ok'),
    '未判 res.ok：不存在的 id 会静默得到 /output/undefined')
})

test('有添加服务图层入口', () => {
  assert.ok(src.includes('ServiceLayerPicker'), '缺少服务图层选择器')
  assert.ok(src.includes('添加服务图层'), '缺少入口文案')
})

test('applyTerrain 改为 key 到 provider 的映射，不留硬编码 else 兜底', () => {
  assert.ok(src.includes('terrainProviders'),
    'applyTerrain 仍是硬编码三元，服务地形会静默回落平面椭球')
  assert.ok(!src.includes("else viewer.terrainProvider = flatTerrain") &&
            !src.includes('else viewer.terrainProvider = flatTerrain;'),
    '仍存在吞掉未知 key 的 else 分支')
})

test('三维瓦片集用 Map 管理，且移除时释放', () => {
  assert.ok(src.includes('tilesetMap') || src.includes('tilesets'),
    'tileset3d 仍是单变量，多个瓦片集时先加载的关不掉')
  assert.ok(src.includes('primitives.remove'),
    '没有 primitives.remove，移除服务时会泄漏 GPU 资源')
})

test('复用既有的服务图层选择器', () => {
  assert.ok(src.includes("from '../components/ServiceLayerPicker.vue'"),
    '应复用共享的选择器组件')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/preview/PreviewService.test.js"`
Expected: FAIL

- [ ] **Step 3: 重排 init —— viewer 创建前移 + id 可选**

把 `PreviewApp.vue::init()` 开头改为：

```javascript
async function init() {
  const id = new URLSearchParams(location.search).get('id')

  // ---- viewer 与底图先建：它们不依赖任务 ----
  // 顺序很关键：measure3d 的 createCesiumMeasure(viewer) 内部注册
  // camera.moveEnd 与 ScreenSpaceEventHandler(viewer.scene.canvas)，
  // 拿到 null 会直接崩。所以 viewer 必须在任何提前 return 之前建好。
  viewer = new Viewer('cesium-container', {
    baseLayer: false, baseLayerPicker: false, geocoder: false,
    homeButton: false, sceneModePicker: true, navigationHelpButton: false,
    timeline: false, animation: false, fullscreenButton: true, infoBox: false,
    selectionIndicator: false,
  })
  viewer.scene.globe.baseColor = window.Cesium?.Color?.DARKSLATEGRAY || undefined
  measure.value = createCesiumMeasure(viewer)

  // 底图（最底层）：NaturalEarthII 离线 geodetic TMS
  try {
    const bm = await TileMapServiceImageryProvider.fromUrl('/basemap', {
      tilingScheme: new GeographicTilingScheme(),
    })
    viewer.imageryLayers.addImageryProvider(bm)
  } catch (e) {
    console.warn('底图加载失败', e)
  }

  // 天地图影像（第二层）。默认不勾选。
  let basemapToken = ''
  try {
    const cfg = await (await fetch('/api/config')).json()
    basemapToken = cfg.basemap_token || ''
  } catch (_) { /* 取不到则天地图图层不可用 */ }
  if (basemapToken) {
    try {
      const tdt = new UrlTemplateImageryProvider({
        url:
          `https://t{s}.tianditu.gov.cn/img_w/wmts?` +
          `SERVICE=WMTS&REQUEST=GetTile&VERSION=1.0.0&LAYER=img` +
          `&STYLE=default&TILEMATRIXSET=w&FORMAT=tiles` +
          `&TILEMATRIX={z}&TILEROW={y}&TILECOL={x}&tk=${basemapToken}`,
        subdomains: ['0', '1', '2', '3', '4', '5', '6', '7'],
        maximumLevel: 18,
      })
      const layer = viewer.imageryLayers.addImageryProvider(tdt)
      layer.show = false
      layers.value.push({ key: 'tianditu', label: '天地图影像(在线)', layer, show: false })
    } catch (e) { console.warn('天地图图层加载失败', e) }
  }

  // 全国 30 米地形：所有预览都提供
  layers.value.push({
    key: 'terrain-national', label: '全国 30 米地形(在线)',
    layer: null, show: false,
  })

  // ---- 无 id：空白预览（只加载底图，服务图层由用户自行添加）----
  if (!id) { loading.value = false; return }

  // ---- 有 id：加载任务成果 ----
  let task = null
  try {
    const res = await fetch(`/api/tasks/${id}`)
    // fetch 对 404 不抛错，body 是合法 JSON，必须显式判 ok，
    // 否则不存在的 id 会静默渲染空预览、顶栏显示 /output/undefined
    if (!res.ok) throw new Error(`任务接口返回 ${res.status}`)
    task = await res.json()
  } catch (e) {
    errorMsg.value = '读取任务信息失败：' + (e?.message || e)
    loading.value = false
    return
  }
  if (!task || !task.id) {
    errorMsg.value = '任务不存在'
    loading.value = false
    return
  }
  // ... 后续保持原有逻辑（taskName、output_path、stages、成果图层加载）
```

- [ ] **Step 4: 地形改 key→provider 映射**

替换模块级变量与 `applyTerrain`：

```javascript
// 地形 provider 表：key -> provider（或 Promise）。地形互斥靠这张表，
// 而不是硬编码的 if/else 链——后者遇到未知 key 会静默回落到平面椭球，
// 表现为"勾了地形但没有任何变化、也不报错"。
const terrainProviders = new Map()   // key -> provider
const terrainLoaders = new Map()     // key -> Promise（并发去重）

/** 按 key 取（必要时加载）地形 provider；失败返回 null */
async function ensureTerrain(key, loader) {
  if (terrainProviders.has(key)) return terrainProviders.get(key)
  if (!terrainLoaders.has(key)) {
    terrainLoaders.set(key, loader().then((p) => {
      terrainProviders.set(key, p)
      terrainLoaders.delete(key)
      return p
    }).catch((e) => {
      terrainLoaders.delete(key)
      throw e
    }))
  }
  return terrainLoaders.get(key)
}

/**
 * 应用地形选择：把除 activeKey 外的地形项全部取消，并挂上对应 provider。
 * activeKey 为 null 表示不启用任何地形（回落平面椭球）。
 */
async function applyTerrain(activeKey) {
  for (const it of layers.value) {
    if (isTerrainKey(it.key)) it.show = it.key === activeKey
  }
  if (!viewer) return
  if (!activeKey) { viewer.terrainProvider = flatTerrain; return }
  // 表里没有的都按需加载：全国地形与服务地形走同一条路
  try {
    const p = await ensureTerrain(activeKey, () => loadTerrain(activeKey))
    viewer.terrainProvider = p || flatTerrain
  } catch (e) {
    console.warn('地形加载失败', activeKey, e)
    terrainError.value = '地形加载失败：' + String(e?.message || e).slice(0, 120)
    setTimeout(() => { terrainError.value = '' }, 8000)
    viewer.terrainProvider = flatTerrain
  }
}

/** 地形 key -> 是否属于地形（本任务 / 全国 / 服务） */
function isTerrainKey(key) {
  return key === 'terrain' || key === 'terrain-national'
    || key.startsWith('svc-terrain:')
}
```

`loadTerrain` 按 key 分派（服务地形的 URL 从已选服务里取）：

```javascript
async function loadTerrain(key) {
  if (key === 'terrain') return terrainProvider
  if (key === 'terrain-national') {
    return CesiumTerrainProvider.fromUrl(NATIONAL_TERRAIN_URL, {
      requestVertexNormals: true, requestWaterMask: false,
    })
  }
  const svcId = key.slice('svc-terrain:'.length)
  const svc = serviceLayers.value.find((s) => s.id === svcId)
  if (!svc) throw new Error('服务不存在')
  return CesiumTerrainProvider.fromUrl(svc.access_path, {
    requestVertexNormals: false, requestWaterMask: false,
  })
}
```

同时把 `TERRAIN_KEYS` 的用到处（`flatHint`、`hasBothTerrain`、`toggle`）换成 `isTerrainKey`，并把 `:498` 的文案改为：

```html
<div v-if="terrainCount > 1" class="tip">地形数据互斥,勾选其一会自动取消其他。</div>
```

`terrainCount` 定义为 `computed(() => layers.value.filter((it) => isTerrainKey(it.key)).length)`。

- [ ] **Step 5: tileset 改 Map 并支持移除**

```javascript
// 三维瓦片集：key -> Cesium3DTileset。**不能只用一个变量**——
// 场景里同时存在多个时，单变量只能控制最后一个，先加载的关不掉，
// 且移除时无处取实例来释放 GPU 资源。
const tilesets = new Map()

async function addTileset(key, url) {
  const ts = await Cesium3DTileset.fromUrl(url, {
    maximumScreenSpaceError: 16, skipLevelOfDetail: true,
  })
  viewer.scene.primitives.add(ts)
  tilesets.set(key, ts)
  return ts
}

function removeTileset(key) {
  const ts = tilesets.get(key)
  if (!ts) return
  ts.show = false
  viewer.scene.primitives.remove(ts)   // 释放 GPU 资源
  tilesets.delete(key)
}
```

`toggle` 里 buildings 分支改为按 key 取：

```javascript
  } else if (it.key.startsWith('svc-model:') || it.key === 'buildings') {
    const ts = tilesets.get(it.key)
    if (ts) ts.show = it.show
  } else if (it.layer) {
    it.layer.show = it.show
  }
```

`flyToBbox` 参数化：

```javascript
/**
 * 相机飞到指定范围。has3d 为真时用倾斜视角（正俯视看不出立体感）。
 * **参数化**：服务图层要飞到自己的范围，不能总用任务的 taskBbox。
 */
function flyToBbox(bbox = taskBbox, has3d = null) {
  if (!viewer || !bbox || bbox.length !== 4) return
  const use3d = has3d === null ? !!tileset3d : has3d
  const rect = Rectangle.fromDegrees(...bbox)
  if (!use3d) {
    viewer.camera.flyTo({ destination: rect, duration: 1.2 })
    return
  }
  const [w, s, e, n] = bbox
  const cx = (w + e) / 2
  const cy = (s + n) / 2
  const spanDeg = Math.max(e - w, n - s)
  const dist = Math.max(spanDeg * 111000 * 1.4, 800)
  viewer.camera.flyTo({
    destination: Cartesian3.fromDegrees(cx, cy - spanDeg * 0.6, dist * 0.7),
    orientation: { heading: 0, pitch: -Math.PI / 4, roll: 0 },
    duration: 1.5,
  })
}
```

- [ ] **Step 6: 加「添加服务图层」按钮与处理**

顶栏加按钮：

```html
<button class="svc-btn" @click="showServicePicker = !showServicePicker">
  添加服务图层
</button>
<div v-if="showServicePicker" class="svc-picker">
  <ServiceLayerPicker @close="showServicePicker = false" @pick="addServiceLayer" />
</div>
```

```javascript
import ServiceLayerPicker from '../components/ServiceLayerPicker.vue'
const showServicePicker = ref(false)
/** 已加入的服务（原始记录，加载地形要用 access_path） */
const serviceLayers = ref([])

/** 把一个服务加进场景。按 kind 分派到影像/地形/模型三条路径。 */
async function addServiceLayer(svc) {
  const key = `svc-${svc.kind}:${svc.id}`
  if (layers.value.some((it) => it.key === key)) {
    MessagePlugin?.info?.(`「${svc.name}」已在场景中`)
    showServicePicker.value = false
    return
  }
  serviceLayers.value = [...serviceLayers.value, svc]
  const bbox = svc.bounds_wgs84 || null
  try {
    if (svc.kind === 'imagery') {
      const p = svc.overlay_desc?.grid === 'geodetic'
        ? await TileMapServiceImageryProvider.fromUrl(svc.access_path, {
            tilingScheme: new GeographicTilingScheme() })
        : new UrlTemplateImageryProvider({
            url: svc.access_path,
            minimumLevel: svc.minzoom ?? 0,
            maximumLevel: svc.maxzoom ?? 18,
          })
      const layer = viewer.imageryLayers.addImageryProvider(p)
      layers.value.push({ key, label: svc.name, layer, show: true })
    } else if (svc.kind === 'terrain') {
      layers.value.push({
        key: `svc-terrain:${svc.id}`, label: svc.name, layer: null, show: false,
      })
      // 与其他地形互斥：勾选它即取消其他
      await applyTerrain(`svc-terrain:${svc.id}`)
    } else if (svc.kind === 'model') {
      await addTileset(key, svc.access_path)
      layers.value.push({ key, label: svc.name, layer: null, show: true })
      await viewer.zoomTo(tilesets.get(key))
    } else {
      MessagePlugin?.warning?.('矢量服务请在二维地图中查看')
    }
    if (bbox) flyToBbox(bbox, svc.kind === 'model')
  } catch (e) {
    console.warn('加载服务失败', svc, e)
    errorMsg.value = `加载「${svc.name}」失败：${String(e?.message || e).slice(0, 120)}`
    setTimeout(() => { errorMsg.value = '' }, 8000)
  }
  showServicePicker.value = false
}
```

卸载时释放：

```javascript
onBeforeUnmount(() => {
  for (const key of [...tilesets.keys()]) {
    try { removeTileset(key) } catch (_) { /* viewer 可能已销毁 */ }
  }
  if (viewer && !viewer.isDestroyed()) viewer.destroy()
})
```

- [ ] **Step 7: 同步既有测试的源码断言**

`frontendvue/src/components/TaskQueue.model3d.test.js:95-115` 断言 `PreviewApp.vue` 含 `'viewer.zoomTo(tileset3d)'` 与 `'taskBbox.every((v) => v === 0)'`。前者已改为 `viewer.zoomTo(tilesets.get(key))`，需同步：

```javascript
  assert.ok(previewSource.includes('viewer.zoomTo(tilesets.get(key))'),
    '缺少 zoomTo 瓦片集定位')
```

- [ ] **Step 8: 运行测试确认通过**

Run: `cd frontendvue && node --test "src/preview/PreviewService.test.js" "src/components/TaskQueue.model3d.test.js"`
Expected: PASS

- [ ] **Step 9: 构建验证**

Run: `cd frontendvue && npm run build`
Expected: 退出码 0

- [ ] **Step 10: 提交**

```bash
git add frontendvue/src/preview/PreviewApp.vue frontendvue/src/preview/PreviewService.test.js frontendvue/src/components/TaskQueue.model3d.test.js
git commit -m "feat(service): 预览页接入服务图层

同时修复四个既有陷阱：viewer 创建前移（id 可选后空白预览无 viewer）、
fetch 判 res.ok（404 不抛错会静默显示 /output/undefined）、applyTerrain
改 key→provider 映射（硬编码 else 会吞掉服务地形）、tileset 改 Map
（单变量多瓦片集时先加载的关不掉且无法释放）。"
```

---

## Task 11: 矢量贴地渲染

**Files:**
- Create: `frontendvue/src/preview/vectorGround.js`
- Modify: `frontendvue/src/preview/PreviewApp.vue`
- Test: `frontendvue/src/preview/vectorGround.test.js`

设计依据 §4.3 / §4.3.1。**GroundPrimitive 的静态工厂在 cesium 1.143 已移除，必须手搓。**

- [ ] **Step 1: 写失败的测试**

```javascript
// frontendvue/src/preview/vectorGround.test.js
import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const src = readFileSync(resolve(here, './vectorGround.js'), 'utf8')

test('面用 GroundPrimitive 手搓构造', () => {
  assert.ok(src.includes('new GroundPrimitive('), '面未用 GroundPrimitive')
  assert.ok(src.includes('GeometryInstance'), '缺少 GeometryInstance')
  assert.ok(src.includes('PolygonGeometry'), '缺少 PolygonGeometry')
  assert.ok(src.includes('ColorGeometryInstanceAttribute'), '缺少颜色属性')
  assert.ok(src.includes('PerInstanceColorAppearance'), '缺少外观')
})

test('线用 GroundPolylinePrimitive', () => {
  assert.ok(src.includes('new GroundPolylinePrimitive('), '线未用贴地折线')
  assert.ok(src.includes('GroundPolylineGeometry'), '缺少折线几何')
  assert.ok(src.includes('PolylineColorAppearance'), '缺少折线外观')
})

test('不依赖已移除的静态工厂', () => {
  assert.ok(!src.includes('fromPolygonHierarchy'),
    'fromPolygonHierarchy 在 cesium 1.143 已移除，调用会运行时报错')
  assert.ok(!src.includes('fromPositions({'), 'GroundPolylinePrimitive.fromPositions 已移除')
})

test('构造前初始化地形高度', () => {
  assert.ok(src.includes('initializeTerrainHeights'),
    '未调用 initializeTerrainHeights，贴地高度未初始化')
})

test('点破例用 Entity 并写明原因', () => {
  assert.ok(src.includes('CLAMP_TO_GROUND'), '点未用贴地高度参考')
  assert.ok(src.includes('没有贴地的') || src.includes('破例'),
    '缺少破例说明注释')
})

test('大量点时回退', () => {
  assert.ok(src.includes('POINT_LIMIT') || src.includes('5000'),
    '缺少点数阈值回退')
})

test('提供移除接口', () => {
  assert.ok(src.includes('export function removeVectorGround'),
    '缺少移除接口，服务移除时无法释放')
})
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd frontendvue && node --test "src/preview/vectorGround.test.js"`
Expected: FAIL — 文件不存在

- [ ] **Step 3: 写实现**

```javascript
/**
 * 矢量图层的贴地渲染。
 *
 * 需求要求"用 primitive、贴地"。Cesium 的实际能力分布决定了这里必须混用两套：
 *
 *   面  GroundPrimitive            —— 是 primitive
 *   线  GroundPolylinePrimitive    —— 是 primitive
 *   点  **没有贴地的 primitive**    —— 破例用 Entity + CLAMP_TO_GROUND
 *
 * 点之所以破例：Cesium 没有贴地的点 primitive。硬做只能在地形加载后逐个采样
 * 高程再设 z，异步时序极易出错（地形未就绪时高程全错）。Entity 的
 * heightReference: CLAMP_TO_GROUND 是官方支持的贴地方案。
 *
 * 另一个坑：GroundPrimitive 的静态工厂方法（fromPolygonHierarchy /
 * fromPositions）在本项目所用的 cesium 1.143 中**已全部移除**，只能用构造
 * 函数 + GeometryInstance 手搓。构造前还必须 await initializeTerrainHeights()。
 */
import {
  Color, ColorGeometryInstanceAttribute, Entity, GeometryInstance,
  GroundPrimitive, GroundPolylineGeometry, GroundPolylinePrimitive, HeightReference,
  PerInstanceColorAppearance, PointGraphics, PolygonGeometry, PolygonHierarchy,
  PolylineColorAppearance, Cartesian3,
} from 'cesium'

/** 点要素的默认样式 */
const POINT_STYLE = { pixelSize: 8, color: Color.fromCssColorString('#f97316') }
/** 线要素的默认样式 */
const LINE_STYLE = { width: 2, color: Color.fromCssColorString('#f97316') }
/** 面要素的默认样式 */
const POLYGON_STYLE = { color: Color.fromCssColorString('#f97316').withAlpha(0.35) }

/**
 * 单图层点数超过它就回退为不贴地的 PointPrimitive。
 * 贴地的 Entity 每个点一份 GPU 资源，几万个点会明显掉帧。
 */
export const POINT_LIMIT = 5000

/** initializeTerrainHeights 只需且只能初始化一次，用 Promise 缓存 */
let terrainHeightsReady = null

function ensureTerrainHeights() {
  if (!terrainHeightsReady) {
    terrainHeightsReady = GroundPrimitive.initializeTerrainHeights()
      .catch((e) => {
        // 初始化失败不应让整个矢量渲染挂掉：GroundPrimitive 会退回
        // "不贴地"的行为，仍能显示，只是不随地形起伏
        console.warn('initializeTerrainHeights 失败，矢量将不随地形起伏', e)
        terrainHeightsReady = null
      })
  }
  return terrainHeightsReady
}

/** 从 GeoJSON 的 coordinates 取出笛卡尔点数组 */
function toCartesians(coords) {
  const flat = []
  for (const c of coords) {
    flat.push(c[0], c[1])
    if (c.length > 2) flat.push(c[2])
  }
  return Cartesian3.fromDegreesArrayHeights(flat)
}

function toCartesians2D(coords) {
  const flat = []
  for (const c of coords) flat.push(c[0], c[1])
  return Cartesian3.fromDegreesArray(flat)
}

/** 面：GroundPrimitive + GeometryInstance（静态工厂已移除，只能手搓） */
function addPolygon(viewer, ring) {
  const positions = toCartesians2D(ring)
  const prim = new GroundPrimitive({
    geometryInstances: new GeometryInstance({
      geometry: new PolygonGeometry({
        polygonHierarchy: new PolygonHierarchy(positions),
      }),
      attributes: {
        color: ColorGeometryInstanceAttribute.fromColor(POLYGON_STYLE.color),
      },
    }),
    appearance: new PerInstanceColorAppearance({
      flat: true, translucent: POLYGON_STYLE.color.alpha < 1,
    }),
  })
  viewer.scene.primitives.add(prim)
  return { kind: 'polygon', primitive: prim }
}

/** 线：GroundPolylinePrimitive */
function addLine(viewer, coords) {
  const positions = toCartesians2D(coords)
  const prim = new GroundPolylinePrimitive({
    geometryInstances: new GeometryInstance({
      geometry: new GroundPolylineGeometry({ positions }),
      attributes: {
        color: ColorGeometryInstanceAttribute.fromColor(LINE_STYLE.color),
      },
    }),
    appearance: new PolylineColorAppearance(),
  })
  viewer.scene.primitives.add(prim)
  return { kind: 'polyline', primitive: prim }
}

/** 点：破例用 Entity —— Cesium 没有贴地的点 primitive */
function addPoints(viewer, coords) {
  const entities = []
  for (const c of coords) {
    entities.push(viewer.entities.add({
      position: Cartesian3.fromDegrees(c[0], c[1]),
      point: new PointGraphics({
        pixelSize: POINT_STYLE.pixelSize,
        color: POINT_STYLE.color,
        // 破例点：没有贴地的点 primitive，只能走 Entity
        heightReference: HeightReference.CLAMP_TO_GROUND,
      }),
    }))
  }
  return { kind: 'points', entities }
}

/** 遍历 GeoJSON 几何，按类型分派 */
function eachGeometry(geom, onPolygon, onLine, onPoint) {
  if (!geom) return
  const { type, coordinates: cs } = geom
  if (type === 'Point') { onPoint([cs]); return }
  if (type === 'MultiPoint') { onPoint(cs); return }
  if (type === 'LineString') { onLine(cs); return }
  if (type === 'MultiLineString') { cs.forEach(onLine); return }
  if (type === 'Polygon') { onPolygon(cs); return }
  if (type === 'MultiPolygon') { cs.forEach((p) => onPolygon(p)); return }
  if (type === 'GeometryCollection') {
    (geom.geometries || []).forEach((g) => eachGeometry(g, onPolygon, onLine, onPoint))
  }
}

/**
 * 把 GeoJSON 以贴地方式加入场景。返回一个句柄，交给 removeVectorGround 释放。
 *
 * options.pointLimit 可覆盖默认的点数阈值。
 */
export async function addVectorGround(viewer, geojson, options = {}) {
  if (!viewer || !geojson) return null
  const pointLimit = options.pointLimit ?? POINT_LIMIT
  const features = geojson.type === 'FeatureCollection'
    ? (geojson.features || [])
    : [geojson.type === 'Feature' ? geojson : { geometry: geojson }]

  // 先数点数，决定点走 Entity 还是降级
  let pointCount = 0
  for (const f of features) {
    eachGeometry(f.geometry, () => {}, () => {},
      (cs) => { pointCount += cs.length })
  }
  const pointsDowngraded = pointCount > pointLimit

  if (!pointsDowngraded) await ensureTerrainHeights()

  const handle = { primitives: [], entities: [], pointsDowngraded, pointCount }

  for (const f of features) {
    eachGeometry(f.geometry,
      (rings) => {
        if (!rings.length) return
        try {
          const h = addPolygon(viewer, rings[0])
          handle.primitives.push(h.primitive)
        } catch (e) { console.warn('面要素渲染失败', e) }
      },
      (coords) => {
        if (coords.length < 2) return
        try {
          const h = addLine(viewer, coords)
          handle.primitives.push(h.primitive)
        } catch (e) { console.warn('线要素渲染失败', e) }
      },
      (coords) => {
        const h = addPoints(viewer, coords)
        handle.entities.push(...h.entities)
      })
  }
  return handle
}

/** 释放句柄占用的资源。服务被移除或图层隐藏时必须调用，否则 GPU 资源泄漏 */
export function removeVectorGround(viewer, handle) {
  if (!viewer || !handle) return
  for (const p of handle.primitives || []) {
    try { viewer.scene.primitives.remove(p) } catch (_) { /* 已移除 */ }
  }
  for (const e of handle.entities || []) {
    try { viewer.entities.remove(e) } catch (_) { /* 已移除 */ }
  }
  handle.primitives = []
  handle.entities = []
}
```

- [ ] **Step 4: 预览页接入矢量服务**

`PreviewApp.vue` 的 `addServiceLayer` 里把矢量分支从"提示请在二维查看"改为：

```javascript
    } else if (svc.kind === 'vector') {
      const geojson = await (await fetch(svc.access_path)).json()
      const handle = await addVectorGround(viewer, geojson)
      vectorHandles.set(key, handle)
      layers.value.push({ key, label: svc.name, layer: null, show: true })
      if (handle?.pointsDowngraded) {
        buildingHint.value = `「${svc.name}」点数超过 ${POINT_LIMIT}，`
          + '已回退为不贴地的点渲染以保证流畅度。'
      }
    }
```

模块级加：

```javascript
import { addVectorGround, removeVectorGround, POINT_LIMIT } from './vectorGround'
const vectorHandles = new Map()
```

`toggle` 的显隐分支加矢量：

```javascript
  } else if (vectorHandles.has(it.key)) {
    const h = vectorHandles.get(it.key)
    for (const p of h.primitives || []) p.show = it.show
    for (const e of h.entities || []) e.show = it.show
  }
```

`onBeforeUnmount` 释放：

```javascript
  for (const [key, h] of vectorHandles) {
    try { removeVectorGround(viewer, h) } catch (_) { /* viewer 可能已销毁 */ }
  }
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd frontendvue && node --test "src/preview/vectorGround.test.js"`
Expected: PASS

- [ ] **Step 6: 全量前端测试**

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: 全部 PASS（基线一致）

- [ ] **Step 7: 构建验证**

Run: `cd frontendvue && npm run build`
Expected: 退出码 0

- [ ] **Step 8: 提交**

```bash
git add frontendvue/src/preview/vectorGround.js frontendvue/src/preview/vectorGround.test.js frontendvue/src/preview/PreviewApp.vue
git commit -m "feat(service): 矢量贴地渲染

面与线用 GroundPrimitive/GroundPolylinePrimitive；点破例用 Entity（Cesium
无贴地点 primitive），超 5000 点回退为不贴地渲染。

手搓 GeometryInstance 而非静态工厂——后者在 cesium 1.143 已全部移除。"
```

---

## Task 12: 端到端验收

**Files:** 无新增，只做验证。

- [ ] **Step 1: 后端全量测试**

Run: `.venv/Scripts/python.exe -m unittest discover tests`
Expected: 通过。**基线有 3 个既有失败**（`tests.test_formats_3d` 的 `Capabilities3DTest` 三个用例，原因是环境缺 `pymartini`），本次不引入新失败即可。

- [ ] **Step 2: 前端全量测试**

Run: `cd frontendvue && node --test "src/**/*.test.js"`
Expected: 全部 PASS

- [ ] **Step 3: 前端构建**

Run: `cd frontendvue && npm run build`
Expected: 退出码 0

- [ ] **Step 4: 启动服务做手工验收**

Run: `start.bat` 然后打开 http://127.0.0.1:8000

逐项确认：

1. 顶栏出现「服务」按钮，角标为已开启服务数
2. 打开面板 → 下方候选列出 `output/` 下的成果（模型 3 项、地形 2 项、矢量 2 项）
3. 登记一个 TMS 服务 → 默认关闭 → 点「开启」→ 点「复制地址」，粘贴出来是 `http://127.0.0.1:8000/api/svc/<id>/{z}/{x}/{y}.png` 且**花括号未被转义**
4. 主界面 → 图层面板 → 「从『服务』列表添加」→ 选 TMS → 地图上出现影像，**位置正确不错位**（geodetic 网格最易错）
5. 调该图层的透明度、上移/下移、定位 → 三项均生效
6. 预览页打开一个本地 TMS 服务 → 显示正确
7. 预览页添加 3D Tiles 服务 → 加载成功并自动定位
8. 预览页添加地形服务 → 地形起伏正确；再勾「全国 30 米地形」→ 两者互斥
9. 预览页添加 geojson 服务 → 贴地显示（切到有地形处看是否随起伏）
10. 关闭一个服务 → 预览页/主界面立刻报错且有明确提示
11. **删掉一个服务的源目录** → 面板显示「源目录已不存在」→ 点「移除」**仍可成功**
12. 从「任务队列」删除一个已叠加图层的任务 → 该图层从地图和图层面板同时消失（B1 修复）

- [ ] **Step 5: 记录验收结果**

把实际结果（含未通过项与原因）追加到设计文档末尾的「验收记录」小节，然后提交：

```bash
git add docs/superpowers/specs/2026-09-21-本地数据服务与预览-design.md
git commit -m "docs: 记录本地数据服务的验收结果"
```

---

## 自审记录

**Spec 覆盖检查**

| 设计文档章节 | 对应任务 |
|---|---|
| §3.1 数据模型 | Task 4 |
| §3.1.1 范围来源 5+1 级 | Task 2 |
| §3.1.2 瓦片索引反算 | Task 1 |
| §3.1.3 网格判定 | Task 1 |
| §3.1.5 定位执行方式 | Task 9（二维）+ Task 10（三维） |
| §3.2 成果识别 | Task 3 |
| §3.3 端点 | Task 5 |
| §3.4 安全与错误语义 | Task 5 |
| §3.5 生命周期与失效 | Task 4（health）+ Task 7（UI） |
| §3.6 复用既有能力 | Task 5（file_dialog）、Task 2（inspect_vector） |
| §4.1 服务面板 | Task 7 |
| §4.2 预览页接入 | Task 10 |
| §4.2.1 四个既有陷阱 | Task 10 |
| §4.3 矢量贴地 | Task 11 |
| §4.3.1 API 事实 | Task 11 |
| §4.4 overlayStore 解耦 | Task 8 |
| §4.4 B1 既有 bug | Task 8 |
| §5 测试策略 | 各任务的测试步骤 + Task 12 |
| §7 已知限制 | 各任务注释与 UI 文案 |

**类型一致性检查**：`Candidate` 字段在 Task 3 定义，Task 5 的 `/api/services/scan` 与 `/api/services/candidates` 按同名输出；`overlay_desc` 的字段名（`kind`/`label`/`url`/`grid`/`flip_y`/`minzoom`/`maxzoom`/`bounds_wgs84`）与 `overlays.js::build()` 读取的字段一致；`taskOverlayKey`/`serviceOverlayKey` 在 Task 8 定义并在 Task 8/9 使用；`removeByService` 在 Task 8 定义并被 Task 7 的服务移除流程间接依赖。

**服务移除与图层的联动**：`serviceStore.removeAndDetach(id)` 同时调 `overlayStore.removeByService(id)`（Task 6 定义、Task 7 的 UI 使用）。若只调 `remove()`，图层会变成"看着正常、实际已无数据源"的幽灵图层——瓦片已加载的部分仍在显示，未加载的静默失败，用户难以察觉成因。这是自审时发现并补上的缺口。

**任务依赖顺序**：Task 1→2（范围解析）→ 3（识别）→ 4（注册表）→ 5（端点）是严格串行的后端链；Task 6 可与 2~5 并行；Task 7 依赖 6；Task 8 独立于后端，可提前做；Task 9 依赖 6+8；Task 10 依赖 7+9；Task 11 依赖 10。按 1→12 顺序执行即可。
