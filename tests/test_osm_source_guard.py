"""OSM 源的两条护栏(最终审查的 Critical 1 与 Important 5)。

**Critical 1**:`_w` 缓存为空时,`_build_osm_source` 会拼出一张**全零**源,
`export_osm` 把每张瓦片判为"无覆盖"直接跳过 → 空目录 + 报成功。
触发路径很现实:升级后重跑一个升级前建的天地图 osm 任务(download 阶段已 done,
`_w` 从没下过)→ 用户拿到空目录且零报错。与需求38-2 是同一个失败形状。

**Important 5**(非本次引入):`_downloaded_raster_source` 硬编码 `range_for_bbox`
(4326),而 `tile_path` 用的是**任务主网格**的 key。Google 只勾 tms 时会落到这条
路径 → 用 4326 行号读 3857 命名的缓存 → 因两套行号范围重叠而**不报错、静默拼出
别处像素**。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import rasterio
from rasterio.transform import from_origin

import backend.core.runner as runner
from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import (
    _build_osm_source, _downloaded_raster_source, _grids_missing_cache,
)

_BBOX = (114.2, 30.4, 114.6, 30.8)
_Z = 6


def _empty_mosaic(path: Path, crs: str) -> None:
    """写一张**全零**栅格(模拟"缓存里一张瓦片都没有"时拼出来的空源)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="GTiff", height=8, width=8, count=3,
                       dtype="uint8", crs=crs,
                       transform=from_origin(114.2, 30.8, 0.05, 0.05)) as dst:
        dst.write(np.zeros((3, 8, 8), dtype=np.uint8))


class OsmEmptySourceGuardTest(unittest.TestCase):
    """★ Critical 1 ★ 空源必须报错,不能静默产出空成果。"""

    def _ctx(self, out_dir, tmp):
        return SimpleNamespace(
            task={"name": "任务", "provider": "tianditu_img"},
            out_dir=out_dir, bbox=_BBOX, geom=None,
            should_stop=lambda: False,
            tracker=SimpleNamespace(update=lambda *a, **k: None),
        )

    def test_empty_source_raises(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            # 源是空的(缓存里没有瓦片时拼出来的就是这张)
            with rasterio.open(
                    root / "src.tif", "w", driver="GTiff", height=8, width=8,
                    count=3, dtype="uint8", crs="EPSG:3857",
                    transform=from_origin(0, 8, 1, 1)) as dst:
                dst.write(np.zeros((3, 8, 8), dtype=np.uint8))

            class _ZeroProvider:
                key, ext, bands = "tianditu_img_w", "jpg", 3

            with self.assertRaises(RuntimeError) as cm:
                _build_osm_source(self._ctx(root, root), _Z, _ZeroProvider(),
                                  lambda col, row, z: root / "none.png",
                                  GEO_MERCATOR, root / "out.tif", None)
            self.assertIn("空", str(cm.exception),
                          "错误文案应说清是源为空(便于定位到'_w 缓存没下过')")

    def test_nonempty_source_is_not_rejected(self):
        """反向护栏:有数据的源不能被误拦(哪怕很暗)。"""
        with TemporaryDirectory() as d:
            root = Path(d)

            class _Prov:
                key, ext, bands = "t1", "png", 3

            def tile_path(col, row, z):
                p = root / "tiles" / f"{col}_{row}.png"
                p.parent.mkdir(parents=True, exist_ok=True)
                if not p.exists():
                    with rasterio.open(p, "w", driver="PNG", height=8, width=8,
                                       count=3, dtype="uint8") as dst:
                        dst.write(np.full((3, 8, 8), 3, dtype=np.uint8))
                return p

            runner.settings.download.cache_dir = str(root / "tiles")
            try:
                got, _ = _build_osm_source(self._ctx(root, root), _Z, _Prov(),
                                           tile_path, GEO_MERCATOR,
                                           root / "out.tif", None)
            finally:
                pass
            self.assertTrue(got.exists())


class GridsMissingCacheTest(unittest.TestCase):
    """★ Critical 1 的另一半 ★ 能判断"某个要求的网格缓存里没有数据"。"""

    def test_detects_missing_grid(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            (root / "tianditu_img_c" / str(_Z)).mkdir(parents=True)
            (root / "tianditu_img_c" / str(_Z) / "1_1.jpg").write_bytes(b"x")
            missing = _grids_missing_cache(root, "tianditu_img",
                                           [GEO_GEODETIC, GEO_MERCATOR], [_Z])
            self.assertEqual(missing, [GEO_MERCATOR],
                             "只有 _c 有缓存时应报出 _w 缺失")

    def test_none_missing_when_both_present(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            for suffix in ("_c", "_w"):
                (root / f"tianditu_img{suffix}" / str(_Z)).mkdir(parents=True)
                (root / f"tianditu_img{suffix}" / str(_Z) / "1_1.jpg"
                 ).write_bytes(b"x")
            self.assertEqual(
                _grids_missing_cache(root, "tianditu_img",
                                     [GEO_GEODETIC, GEO_MERCATOR], [_Z]), [])


class DownloadedRasterSourceGridTest(unittest.TestCase):
    """★ Important 5 ★ 该自拼源要按**任务主网格**取范围与 crs。"""

    def test_mercator_task_builds_3857_source(self):
        with TemporaryDirectory() as d:
            root = Path(d)
            cache = root / "tiles"
            from backend.core.mercator_tiling import mercator_range_for_bbox
            tr = mercator_range_for_bbox(*_BBOX, _Z)
            p = (cache / "google_img" / str(_Z)
                 / f"{tr.col_min}_{tr.row_min}.jpg")
            p.parent.mkdir(parents=True, exist_ok=True)
            with rasterio.open(p, "w", driver="PNG", height=8, width=8,
                               count=3, dtype="uint8") as dst:
                dst.write(np.full((3, 8, 8), 200, dtype=np.uint8))

            ctx = SimpleNamespace(
                task={"name": "任务", "provider": "google_img"},
                out_dir=root / "out", bbox=_BBOX, grid=GEO_MERCATOR,
                geom=None, anno_downloader=None, cur_stage="tms",
                should_stop=lambda: False,
                tracker=SimpleNamespace(update=lambda *a, **k: None),
                provider=SimpleNamespace(key="google_img", ext="jpg", bands=3),
                downloader=SimpleNamespace(
                    tile_path=lambda col, row, z:
                        cache / "google_img" / str(z) / f"{col}_{row}.jpg"),
            )
            (root / "out").mkdir()
            runner.settings.download.cache_dir = str(cache)
            got = _downloaded_raster_source(ctx, _Z)
            with rasterio.open(got) as s:
                self.assertEqual(s.crs.to_string(), "EPSG:3857",
                                 "墨卡托任务的自拼源应是 3857(交给下游重投影),"
                                 "按 4326 行列号读 3857 缓存会静默拼出别处像素")
                self.assertGreater(int(s.read().max()), 0, "拼出来是空的")


if __name__ == "__main__":
    unittest.main()
