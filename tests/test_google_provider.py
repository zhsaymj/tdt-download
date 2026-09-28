"""Google 影像 provider(4 图层,EPSG:3857 墨卡托 XYZ)。"""
import unittest

from backend.config import GoogleConfig
from backend.providers.google import (
    GOOGLE_LAYERS, build_google_provider, is_google_provider,
)


def _cfg(**kw) -> GoogleConfig:
    base = GoogleConfig(enabled=True, proxy="127.0.0.1:6789")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


class TestLayerRegistry(unittest.TestCase):
    def test_four_layers(self):
        self.assertEqual(set(GOOGLE_LAYERS), {
            "google_img", "google_hybrid", "google_road", "google_terrain"})

    def test_is_google_provider(self):
        self.assertTrue(is_google_provider("google_img"))
        self.assertTrue(is_google_provider("google_road"))
        self.assertFalse(is_google_provider("tianditu_img"))
        self.assertFalse(is_google_provider("esri_imagery"))
        self.assertFalse(is_google_provider(""))


class TestBands(unittest.TestCase):
    def test_satellite_is_three_bands_jpg(self):
        p = build_google_provider("google_img", _cfg())
        self.assertEqual(p.bands, 3)
        self.assertEqual(p.ext, "jpg")

    def test_hybrid_is_three_bands_jpg(self):
        p = build_google_provider("google_hybrid", _cfg())
        self.assertEqual(p.bands, 3)
        self.assertEqual(p.ext, "jpg")

    def test_terrain_is_three_bands_jpg(self):
        p = build_google_provider("google_terrain", _cfg())
        self.assertEqual(p.bands, 3)
        self.assertEqual(p.ext, "jpg")

    def test_road_is_single_band_png(self):
        """实测 lyrs=m 返回 count=1 的调色板 PNG。按 3 波段处理会抛
        DatasetIOShapeError,故必须登记为 1。"""
        p = build_google_provider("google_road", _cfg())
        self.assertEqual(p.bands, 1)
        self.assertEqual(p.ext, "png")


class TestTileUrl(unittest.TestCase):
    def test_lyrs_and_coords_substituted(self):
        p = build_google_provider("google_img", _cfg(subdomains="1"))
        url = p.tile_url(53955, 24810, 16)
        self.assertEqual(
            url, "https://mt1.google.com/vt/lyrs=s&x=53955&y=24810&z=16")

    def test_each_layer_uses_its_lyrs_code(self):
        want = {"google_img": "s", "google_hybrid": "y",
                "google_road": "m", "google_terrain": "p"}
        for key, code in want.items():
            p = build_google_provider(key, _cfg(subdomains="0"))
            self.assertIn(f"lyrs={code}&", p.tile_url(1, 2, 3))

    def test_subdomain_rotates(self):
        p = build_google_provider("google_img", _cfg(subdomains="0,1,2,3"))
        subs = [p.tile_url(1, 1, 5).split("//")[1].split(".")[0]
                for _ in range(5)]
        self.assertEqual(subs, ["mt0", "mt1", "mt2", "mt3", "mt0"])

    def test_custom_url_template_honored(self):
        """端点会变更,模板必须可配置。"""
        p = build_google_provider(
            "google_img",
            _cfg(url_template="https://alt{s}.example.com/t?l={lyrs}&{x}/{y}/{z}",
                 subdomains="9"))
        self.assertEqual(p.tile_url(7, 8, 9),
                         "https://alt9.example.com/t?l=s&7/8/9")

    def test_empty_url_template_rejected(self):
        with self.assertRaises(ValueError):
            build_google_provider("google_img", _cfg(url_template=""))

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            build_google_provider("google_nope", _cfg())


class TestZoomAndProxy(unittest.TestCase):
    def test_max_zoom_from_config(self):
        self.assertEqual(build_google_provider("google_img", _cfg()).max_zoom(), 21)

    def test_max_zoom_configurable(self):
        p = build_google_provider("google_img", _cfg(max_zoom=19))
        self.assertEqual(p.max_zoom(), 19)

    def test_min_zoom_is_one(self):
        self.assertEqual(build_google_provider("google_img", _cfg()).min_zoom(), 1)

    def test_proxy_normalized(self):
        """配置里写 host:port,provider 必须返回带 scheme 的 URL。"""
        p = build_google_provider("google_img", _cfg(proxy="127.0.0.1:6789"))
        self.assertEqual(p.proxy, "http://127.0.0.1:6789")

    def test_proxy_empty_is_none(self):
        p = build_google_provider("google_img", _cfg(proxy=""))
        self.assertIsNone(p.proxy)


class TestEmptyTileSemantics(unittest.TestCase):
    def test_no_placeholder_behavior(self):
        """实测 Google 无 200 占位图:无影像处返回 404(见 missing_statuses)。"""
        p = build_google_provider("google_img", _cfg())
        self.assertFalse(p.is_empty_tile(b"\xff\xd8\xff\xe0anything"))
        self.assertFalse(p.is_empty_tile(b""))

    def test_404_declared_as_missing(self):
        p = build_google_provider("google_img", _cfg())
        self.assertEqual(p.missing_statuses(), frozenset({404}))


if __name__ == "__main__":
    unittest.main()
