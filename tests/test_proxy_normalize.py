"""代理串归一化。

背景:aiohttp 的 proxy 参数必须是带 scheme 的完整 URL,传 "127.0.0.1:6789"
会抛 InvalidURL(实测 aiohttp 3.11.11)。而用户在 config.yaml 里习惯只写
host:port(buildings.proxy 现有配置就是这个形式),故代码侧补 scheme。
"""
import unittest

from backend.providers.base import TileProvider, normalize_proxy


class TestNormalizeProxy(unittest.TestCase):
    def test_bare_host_port_gets_http_scheme(self):
        """核心用例:用户按习惯只写 host:port。"""
        self.assertEqual(normalize_proxy("127.0.0.1:6789"),
                         "http://127.0.0.1:6789")

    def test_existing_scheme_kept(self):
        self.assertEqual(normalize_proxy("http://127.0.0.1:6789"),
                         "http://127.0.0.1:6789")

    def test_https_scheme_kept(self):
        self.assertEqual(normalize_proxy("https://proxy.local:8443"),
                         "https://proxy.local:8443")

    def test_socks5_passed_through_unchanged(self):
        """不静默降级成 http —— 那会连到 SOCKS 端口发 HTTP 请求,报错极难懂。"""
        self.assertEqual(normalize_proxy("socks5://127.0.0.1:1080"),
                         "socks5://127.0.0.1:1080")

    def test_empty_is_none(self):
        self.assertIsNone(normalize_proxy(""))

    def test_none_is_none(self):
        self.assertIsNone(normalize_proxy(None))

    def test_whitespace_only_is_none(self):
        self.assertIsNone(normalize_proxy("   "))

    def test_surrounding_whitespace_stripped(self):
        self.assertEqual(normalize_proxy("  127.0.0.1:6789  "),
                         "http://127.0.0.1:6789")

    def test_hostname_without_port(self):
        self.assertEqual(normalize_proxy("proxy.local"),
                         "http://proxy.local")


class _MinimalProvider(TileProvider):
    key = "minimal"

    def tile_url(self, col, row, z):
        return "https://example.com/tile"


class TestProviderDefaults(unittest.TestCase):
    def test_proxy_defaults_to_none(self):
        """现有数据源(天地图/DEM)不走代理,默认必须是 None。"""
        self.assertIsNone(_MinimalProvider().proxy)

    def test_missing_statuses_defaults_empty(self):
        """默认空集:天地图与 Esri 用 200+内容表达无数据,不用状态码。"""
        self.assertEqual(_MinimalProvider().missing_statuses(), frozenset())

    def test_tianditu_provider_has_no_proxy(self):
        """回归护栏:不能让天地图被意外绕进代理。"""
        from backend.providers.tianditu import build_provider
        p = build_provider("tianditu_img", "dummy-token")
        self.assertIsNone(p.proxy)
        self.assertEqual(p.missing_statuses(), frozenset())

    def test_terrain_provider_has_no_proxy(self):
        """Esri Terrain3D 直连可用,同样不应走代理。"""
        from backend.providers.terrain import build_terrain_provider
        p = build_terrain_provider("esri_terrain")
        self.assertIsNone(p.proxy)


if __name__ == "__main__":
    unittest.main()
