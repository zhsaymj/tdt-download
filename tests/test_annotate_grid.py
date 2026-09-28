"""注记 provider 的按源配对与网格选择。

Google/Esri 是影像源,配影像注记(cia)。它们走 3857 网格,故注记 provider
必须用 matrix_set="w" —— 否则行列号按 3857 算、URL 却请求 4326 瓦片,
取回的是**另一个地方**的注记(不报错,只是路网对不上影像)。
"""
import unittest

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.providers.tianditu import (
    ANNOTATION_OF, build_annotation_provider,
)


class TestAnnotationOf(unittest.TestCase):
    def test_tianditu_mappings_unchanged(self):
        """回归护栏:天地图三项映射与改动前一致。"""
        self.assertEqual(ANNOTATION_OF["tianditu_img"], "tianditu_cia")
        self.assertEqual(ANNOTATION_OF["tianditu_vec"], "tianditu_cva")
        self.assertEqual(ANNOTATION_OF["tianditu_ter"], "tianditu_cta")

    def test_google_and_esri_map_to_imagery_annotation(self):
        """Google/Esri 都是影像源 → 配影像注记 cia。"""
        for k in ("google_img", "google_hybrid", "google_road",
                  "google_terrain", "esri_imagery"):
            self.assertEqual(ANNOTATION_OF.get(k), "tianditu_cia", k)


class TestBuildAnnotationProvider(unittest.TestCase):
    def test_geodetic_uses_c(self):
        p = build_annotation_provider("tianditu_img", "tk",
                                      grid=GEO_GEODETIC)
        self.assertIsNotNone(p)
        self.assertEqual(p.matrix_set, "c")

    def test_mercator_uses_w(self):
        p = build_annotation_provider("google_img", "tk",
                                      grid=GEO_MERCATOR)
        self.assertIsNotNone(p)
        self.assertEqual(p.matrix_set, "w")
        self.assertIn("/cia_w/wmts?", p.tile_url(1, 1, 16))

    def test_esri_imagery_uses_w(self):
        p = build_annotation_provider("esri_imagery", "tk",
                                      grid=GEO_MERCATOR)
        self.assertEqual(p.matrix_set, "w")

    def test_default_grid_is_geodetic(self):
        """不传 grid 时回落 c —— 现有调用点行为不变。"""
        self.assertEqual(
            build_annotation_provider("tianditu_img", "tk").matrix_set, "c")

    def test_unknown_source_returns_none(self):
        """无配对注记的源返回 None(现有语义)。"""
        for k in ("esri_terrain", "local_image", "osm_buildings", ""):
            self.assertIsNone(build_annotation_provider(k, "tk"), k)

    def test_legacy_short_key(self):
        p = build_annotation_provider("img", "tk")
        self.assertIsNotNone(p)
        self.assertEqual(p.key, "tianditu_cia")

    def test_mercator_grid_on_tianditu_source_also_w(self):
        """网格由参数决定,不由源类型决定 —— 天地图源也能用 w(供将来复用)。"""
        p = build_annotation_provider("tianditu_img", "tk", grid=GEO_MERCATOR)
        self.assertEqual(p.matrix_set, "w")


if __name__ == "__main__":
    unittest.main()
