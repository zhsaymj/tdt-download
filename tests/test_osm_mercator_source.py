"""OSM 的源要与 OSM 的输出网格一致(设计 D4 / Task 4)。

`export_osm` 把源包成"虚拟 EPSG:3857 数据集"再切 XYZ 瓦片。源若是 3857,这层
包装就是**空操作**(零重投影);源若是 4326(天地图的下载网格),就得逐瓦片重投影
——有损。

所以:

* 只有 3857 的源(Google/Esri):geotiff 成果本身就是 3857,直接复用
* **两套网格都有(天地图)而任务默认是 geodetic**:geotiff 成果是 4326 的**不能用**,
  要从 `_w` 缓存另拼一份 3857 源 —— 这正是本任务的核心
* 裁剪任务:OSM 需要未裁剪源(自带几何遮罩、能精确切边)
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import rasterio

import backend.core.runner as runner
from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.mercator_tiling import mercator_range_for_bbox
from backend.core.runner import _build_provider_for, _osm_source_for_level

_BBOX = (114.2, 30.4, 114.6, 30.8)
_Z = 6


def _write_tile(path: Path, value: int = 200) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(path, "w", driver="PNG", height=8, width=8, count=3,
                       dtype="uint8") as dst:
        dst.write(np.full((3, 8, 8), value, dtype=np.uint8))


def _write_geotiff(path: Path, crs: str) -> None:
    """写一张小 GeoTIFF,模拟 geotiff 阶段的成果(网格由 crs 决定)。"""
    from rasterio.transform import from_origin
    with rasterio.open(path, "w", driver="GTiff", height=8, width=8, count=3,
                       dtype="uint8", crs=crs,
                       transform=from_origin(0, 8, 1, 1)) as dst:
        dst.write(np.full((3, 8, 8), 7, dtype=np.uint8))


class _CtxFactory(unittest.TestCase):
    def _ctx(self, out_dir, task, provider, cache, grid, anno=None):
        return SimpleNamespace(
            task=task, out_dir=out_dir, provider=provider, grid=grid,
            bbox=_BBOX, geom=None, anno_downloader=anno,
            should_stop=lambda: False,
            tracker=SimpleNamespace(update=lambda *a, **k: None),
            downloader=SimpleNamespace(
                tile_path=lambda col, row, z:
                    cache / provider.key / str(z) / f"{col}_{row}.{provider.ext}"),
        )

    def _use_cache(self, cache: Path):
        self._orig = runner.settings.download.cache_dir
        runner.settings.download.cache_dir = str(cache)

    def tearDown(self):
        if hasattr(self, "_orig"):
            runner.settings.download.cache_dir = self._orig


class TiandituOsmSourceTest(_CtxFactory):
    def test_source_is_mercator_when_provider_has_two_grids(self):
        """★ 核心 ★ 天地图任务:OSM 源要来自 `_w` 缓存、产出 3857。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            cache, out = root / "tiles", root / "out"
            out.mkdir()
            provider = _build_provider_for({"provider": "tianditu_img"},
                                           grid=GEO_MERCATOR)
            # 往 `_w` 网格的缓存里铺满该范围的瓦片
            tr = mercator_range_for_bbox(*_BBOX, _Z)
            for col in range(tr.col_min, tr.col_max + 1):
                for row in range(tr.row_min, tr.row_max + 1):
                    _write_tile(cache / provider.key / str(_Z)
                                / f"{col}_{row}.{provider.ext}")
            self._use_cache(cache)

            ctx = self._ctx(out, {"name": "任务", "provider": "tianditu_img"},
                            _build_provider_for({"provider": "tianditu_img"}),
                            cache, GEO_GEODETIC)
            got, reusable = _osm_source_for_level(ctx, _Z)
            self.assertTrue(got.exists(), "未产出 OSM 源")
            with rasterio.open(got) as s:
                self.assertEqual(s.crs.to_string(), "EPSG:3857",
                                 "OSM 源必须是 3857 —— 否则 export_osm 要逐瓦片重投影")
                self.assertGreater(int(s.read().max()), 0,
                                   "拼出来是空的 —— 说明没读到 _w 缓存")

    def test_does_not_reuse_geodetic_geotiff_output(self):
        """★ 核心 ★ 天地图的 geotiff 成果是 4326 的,**不能**拿来当 OSM 源。

        若误用,`export_osm` 就会重投影 —— 那正是本任务要消除的损失。
        """
        with TemporaryDirectory() as d:
            root = Path(d)
            cache, out = root / "tiles", root / "out"
            out.mkdir()
            # geotiff 成果在(4326),但 _w 缓存也有
            _write_geotiff(out / "任务_z6.tif", "EPSG:4326")
            provider = _build_provider_for({"provider": "tianditu_img"},
                                           grid=GEO_MERCATOR)
            tr = mercator_range_for_bbox(*_BBOX, _Z)
            for col in range(tr.col_min, tr.col_max + 1):
                for row in range(tr.row_min, tr.row_max + 1):
                    _write_tile(cache / provider.key / str(_Z)
                                / f"{col}_{row}.{provider.ext}")
            self._use_cache(cache)

            ctx = self._ctx(out, {"name": "任务", "provider": "tianditu_img"},
                            _build_provider_for({"provider": "tianditu_img"}),
                            cache, GEO_GEODETIC)
            got, _ = _osm_source_for_level(ctx, _Z)
            self.assertNotEqual(got, out / "任务_z6.tif",
                                "不该复用 4326 的 geotiff 成果作 OSM 源")
            with rasterio.open(got) as s:
                self.assertEqual(s.crs.to_string(), "EPSG:3857")


class GoogleOsmSourceTest(_CtxFactory):
    def test_mercator_provider_reuses_geotiff_output(self):
        """回归:Google/Esri 只有 3857,geotiff 成果就是 3857 源,直接复用。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            cache, out = root / "tiles", root / "out"
            out.mkdir()
            _write_geotiff(out / "任务_z6.tif", "EPSG:3857")
            provider = _build_provider_for({"provider": "google_img"})
            ctx = self._ctx(out, {"name": "任务", "provider": "google_img"},
                            provider, cache, GEO_MERCATOR)
            got, reusable = _osm_source_for_level(ctx, _Z)
            self.assertEqual(got, out / "任务_z6.tif")
            self.assertTrue(reusable, "复用的成果不该在切完后被删掉")


if __name__ == "__main__":
    unittest.main()
