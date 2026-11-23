"""根路径入口(ui.default_page)的回归护栏。

地形切片版与主下载工具共用同一份产物,差异只在 config.yaml 的
`ui.default_page` 决定 `/` 跳到哪个页面。这里钉住三件事:

1. `/` 真的按配置重定向(而不是被 catch-all 的 StaticFiles 直接吃掉);
2. 非法配置值回落 index,且不会把配置值拼进路径(防穿越);
3. 两个页面在任何配置下都能直接访问 —— 否则「主下载工具」链接
   指向 /index.html 会 404。
"""
import unittest

from fastapi.testclient import TestClient


class RootRedirectTest(unittest.TestCase):
    def setUp(self):
        from backend import config
        from backend.main import app
        # TestClient 默认跟随重定向,这里要断言 302 本身
        self.client = TestClient(app, follow_redirects=False)
        self._old = config.settings.ui.default_page
        self.addCleanup(self._restore)

    def _restore(self):
        from backend import config
        config.settings.ui.default_page = self._old

    def _set_page(self, value):
        from backend import config
        config.settings.ui.default_page = value

    def test_root_default_redirects_to_index(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/index.html")

    def test_root_redirects_to_terrain_when_configured(self):
        self._set_page("terrain")
        r = self.client.get("/")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(r.headers["location"], "/terrain.html")

    def test_root_falls_back_to_index_when_configured(self):
        self._set_page("index")
        r = self.client.get("/")
        self.assertEqual(r.headers["location"], "/index.html")

    def test_invalid_value_falls_back_to_index(self):
        for bad in ("bogus", "", None, "/etc/passwd", "../index", "../../secret",
                    "index.html?x=1", "terrain/../../etc/passwd"):
            with self.subTest(value=bad):
                self._set_page(bad)
                r = self.client.get("/")
                self.assertEqual(r.status_code, 302)
                self.assertEqual(r.headers["location"], "/index.html")

    def test_value_is_normalized(self):
        # 大小写与首尾空白是配置文件里最常见的写法差异,不该导致回落
        for good in ("Terrain", "TERRAIN", "  terrain  "):
            with self.subTest(value=good):
                self._set_page(good)
                r = self.client.get("/")
                self.assertEqual(r.headers["location"], "/terrain.html")

    def test_both_pages_directly_reachable(self):
        """两页必须始终可达:地形页的「主下载工具」链接指 /index.html。"""
        for page in ("/index.html", "/terrain.html"):
            with self.subTest(page=page):
                r = self.client.get(page)
                self.assertEqual(r.status_code, 200)
                self.assertIn("text/html", r.headers["content-type"])

    def test_root_route_does_not_shadow_api(self):
        """根路由注册在 mount("/") 之前,不能顺带截胡 /api 与 /ws。"""
        r = self.client.get("/api/logs")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn("location", r.headers)


if __name__ == "__main__":
    unittest.main()
