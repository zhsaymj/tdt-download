"""mosaic_to_geotiff 的 crs 参数。

护栏重点:默认参数下的输出必须与改动前完全一致(天地图现有行为不能变)。
"""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio

from backend.core.mercator_tiling import (
    mercator_range_for_bbox, mosaic_bounds_3857,
)
from backend.core.mosaic import mosaic_to_geotiff
from backend.core.tiling import range_for_bbox


class _FakeProvider:
    """最小 provider:拼接只用到 bands。"""
    key = "fake_img"
    ext = "tif"
    bands = 3


def _write_tile(path: Path, value: int) -> None:
    """写一张 256x256 的纯色 GeoTIFF 作为瓦片。

    刻意用 GTiff 而非 PNG:rasterio 写 PNG 需走 CreateCopy,在本测试里
    没有额外价值;mosaic 的 _read_tile 用 rasterio.open 读,格式无关。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((3, 256, 256), value, dtype=np.uint8)
    profile = {"driver": "GTiff", "height": 256, "width": 256,
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
            return cache / str(z) / f"{col}_{row}.tif"
        return fn

    def _prepare(self, tr, cache: Path):
        for col in range(tr.col_min, tr.col_max + 1):
            for row in range(tr.row_min, tr.row_max + 1):
                _write_tile(cache / str(tr.z) / f"{col}_{row}.tif", 128)

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
