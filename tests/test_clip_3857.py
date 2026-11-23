"""裁剪对 3857 源的支持。

背景:clip_to_geometry 原先假定源是 EPSG:4326,把 WGS84 几何坐标直接当源图
坐标用。对 3857 拼接图(坐标是米,±2e7 量级),几何(±180 量级)会被判成
"与影像无重叠",静默返回 False —— 图一点没裁,用户看不出哪里错了。
"""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
import rasterio
import rasterio.features
from rasterio.transform import from_bounds

from backend.core.mercator_tiling import (
    mercator_range_for_bbox, mosaic_bounds_3857,
)
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


class TestClipStreaming(unittest.TestCase):
    def test_clip_is_chunked_and_bounded(self):
        """29137×46002 OOM 回归:几何掩膜必须按窗口分块,不能整幅分配。"""
        bounds = (0.0, 0.0, 1.0, 1.0)
        geom = {
            "type": "Polygon",
            "coordinates": [[[0.2, 0.2], [0.8, 0.2], [0.8, 0.8],
                             [0.2, 0.8], [0.2, 0.2]]],
        }
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "stream.tif"
            _make_tif(p, bounds, "EPSG:4326", size=64)
            shapes = []
            original = rasterio.features.geometry_mask

            def spy(*args, **kwargs):
                shapes.append(kwargs["out_shape"])
                return original(*args, **kwargs)

            with patch("rasterio.features.geometry_mask", side_effect=spy):
                self.assertTrue(clip_to_geometry(p, geom, chunk_px=7))
            self.assertGreater(len(shapes), 1)
            self.assertTrue(all(h <= 7 and w <= 7 for h, w in shapes),
                            f"出现整幅掩膜 {shapes}")
            with rasterio.open(p) as ds:
                alpha = ds.read(4)
            self.assertEqual(int(alpha.min()), 0)
            self.assertEqual(int(alpha.max()), 255)


if __name__ == "__main__":
    unittest.main()
