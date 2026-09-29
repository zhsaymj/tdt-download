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

    def test_legacy_short_key(self):
        """兼容早期落库的短 key,与 kind_of 的处理一致。"""
        self.assertEqual(grid_of("img"), GEO_GEODETIC)


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
