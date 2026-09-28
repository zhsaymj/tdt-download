"""runner 的网格分流:provider 构造、瓦片区间函数、拼接坐标系。

不跑真实下载,只验证"按 provider 选对了哪套函数/参数"。
"""
import unittest

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import (
    _build_provider_for, _crs_for_grid, _range_fn_for,
)


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
        import backend.core.runner as mod
        # 天地图需要密钥;测试环境可能未配,注入一个假的
        orig = mod.settings.tianditu.token
        mod.settings.tianditu.token = "dummy-token"
        try:
            p = _build_provider_for(_Task(provider="tianditu_img"))
        finally:
            mod.settings.tianditu.token = orig
        self.assertIsInstance(p, TiandituProvider)

    def test_google_road_bands_is_one(self):
        """端到端确认:构造出来的 provider 波段数正确(拼接靠它)。"""
        p = _build_provider_for(_Task(provider="google_road"))
        self.assertEqual(p.bands, 1)

    def test_all_google_layers_constructible(self):
        for k in ("google_img", "google_hybrid", "google_road", "google_terrain"):
            p = _build_provider_for(_Task(provider=k))
            self.assertEqual(p.key, k)


class TestNoWorkerChanges(unittest.TestCase):
    """回归护栏:新 provider 必须走 2D runner,不能被路由到 runner_3d。"""

    def test_resolve_runner_routes_to_2d(self):
        from backend.core.formats import is_3d_provider
        for k in ("google_img", "google_road", "esri_imagery"):
            self.assertFalse(is_3d_provider(k), k)


if __name__ == "__main__":
    unittest.main()
