import unittest


class FormatDefaultTest(unittest.TestCase):
    def test_geotiff_stages_default_to_cog_container(self):
        from backend.core.containers import container_of

        self.assertEqual(container_of({"containers": {}}, "geotiff"), "cog")
        self.assertEqual(container_of({"containers": {}}, "dem"), "cog")


class TileGridDefaultTest(unittest.TestCase):
    """瓦片格式的默认勾选要按**数据源自己的网格**选(需求37)。

    天地图是 EPSG:4326,TMS(gdal2tiles geodetic)与它同构 → 无损直映射;
    Google/Esri 是 EPSG:3857,出 geodetic TMS 必须先重投影 —— 实测高频能量
    只剩 68%(bilinear),前端显示时 OL 还要再转一次 3857,端到端约 44%,
    细笔画的文字标注明显发虚。OSM 是 Web 墨卡托 XYZ,3857→3857 无损,
    且前端按 3857 渲染、不再重投影。

    所以默认瓦片格式必须随网格走,不能靠注册表里静态的 default_on。
    """

    def test_geodetic_sources_default_to_tms(self):
        from backend.core.formats import default_on_stage_keys
        for p in ("tianditu_img", "tianditu_vec", "tianditu_ter"):
            keys = default_on_stage_keys(p)
            self.assertIn("tms", keys, f"{p} 应默认出 TMS(与源同构、无损)")
            self.assertNotIn("osm", keys, f"{p} 不该默认同时出 OSM")

    def test_mercator_sources_default_to_osm(self):
        from backend.core.formats import default_on_stage_keys
        for p in ("google_img", "esri_imagery"):
            keys = default_on_stage_keys(p)
            self.assertIn("osm", keys, f"{p} 应默认出 OSM(3857,无损)")
            self.assertNotIn("tms", keys,
                             f"{p} 不该默认出 TMS —— 要重投影、文字会发虚")

    def test_other_defaults_untouched(self):
        """只有瓦片格式随网格变;GeoTIFF 等其余默认项不能被误伤。"""
        from backend.core.formats import default_on_stage_keys
        for p in ("tianditu_img", "google_img", "esri_imagery"):
            self.assertIn("geotiff", default_on_stage_keys(p), p)

    def test_unknown_provider_falls_back_to_geodetic(self):
        """未登记的数据源回落 geodetic(与 grid_of 的既有回落一致)。"""
        from backend.core.formats import default_on_stage_keys
        self.assertIn("tms", default_on_stage_keys("img"))


class CapabilitiesGridDefaultTest(unittest.TestCase):
    """/api/capabilities 的 default_on 必须体现上面的网格判定 ——
    前端就是读它决定默认勾选的,不一致的话改了后端也不生效。"""

    def _providers(self):
        import asyncio
        from backend.main import api_capabilities
        return asyncio.run(api_capabilities())["providers"]

    def _default_on(self, providers, provider):
        return {s["key"] for s in providers[provider]["stages"] if s["default_on"]}

    def test_capabilities_matches_grid_rule(self):
        providers = self._providers()
        self.assertIn("tms", self._default_on(providers, "tianditu_img"))
        self.assertIn("osm", self._default_on(providers, "google_img"))
        self.assertNotIn("tms", self._default_on(providers, "google_img"))
        self.assertIn("osm", self._default_on(providers, "esri_imagery"))
        self.assertNotIn("tms", self._default_on(providers, "esri_imagery"))


if __name__ == "__main__":
    unittest.main()
