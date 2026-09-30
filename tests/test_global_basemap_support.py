"""全球底图只对影像/底图类源生效 —— DEM 与本地源必须被拦下(设计 §4)。

不拦的后果是**静默浪费**:DEM 任务的 TMS/OSM 导出走重采样链路
(_tms_from_dem / _tms_from_source_file),全球段根本进不去,但下载阶段
会老老实实下完 geodetic z1-5 的 682 张,预估也把它算进总数。
"""
import unittest

from backend.core.tile_estimate import tile_total
from backend.core.tile_range import supports_global_basemap

BBOX = (120.5, 30.5, 121.0, 30.9)


class SupportsGlobalBasemapTest(unittest.TestCase):
    def test_imagery_supported(self):
        for p in ("tianditu_img", "tianditu_vec", "tianditu_ter",
                  "google_img", "esri_imagery"):
            self.assertTrue(supports_global_basemap(p), p)

    def test_dem_not_supported(self):
        for p in ("esri_terrain", "aws_terrain", "local_dem"):
            self.assertFalse(supports_global_basemap(p), p)

    def test_local_and_buildings_not_supported(self):
        for p in ("local_image", "osm_buildings", "local_vector"):
            self.assertFalse(supports_global_basemap(p), p)


class TileTotalDemGuardTest(unittest.TestCase):
    def test_dem_ignores_global_level(self):
        """★ 核心 ★ DEM 源传了 global_max_level 也当没传(防白下载)。"""
        base = tile_total("esri_terrain", ("tms",), BBOX, [12])
        with_global = tile_total("esri_terrain", ("tms",), BBOX, [12],
                                 global_max_level=5, buffer_rings=1)
        self.assertEqual(with_global, base)

    def test_imagery_honours_global_level(self):
        """影像源则如实生效(与 DEM 那条形成对照)。"""
        base = tile_total("tianditu_img", ("tms",), BBOX, [12])
        with_global = tile_total("tianditu_img", ("tms",), BBOX, [12],
                                 global_max_level=5)
        self.assertGreater(with_global, base)


if __name__ == "__main__":
    unittest.main()