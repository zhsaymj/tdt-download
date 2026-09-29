"""预览瓦片转发端点。

关注点:
  - SSRF 护栏:只接受已登记且 enabled 的 provider key
  - 失败降级:返回透明 PNG 而非 5xx(否则地图破图)
  - 不写缓存:预览与下载用途不同,混用会让下载进度估算失真
"""
import unittest

from backend.api import tiles as tiles_api


class TestProviderWhitelist(unittest.TestCase):
    def test_unknown_provider_rejected(self):
        """防止端点变成任意 URL 代理(SSRF)。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            tiles_api._resolve_preview_provider("../../etc/passwd")
        self.assertEqual(cm.exception.status_code, 404)

    def test_tianditu_not_allowed_here(self):
        """天地图底图前端直连(有自己的 token 机制),不走本端点。"""
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            tiles_api._resolve_preview_provider("tianditu_img")

    def test_disabled_provider_rejected(self):
        from fastapi import HTTPException
        from backend.config import settings
        orig = settings.google.enabled
        settings.google.enabled = False
        try:
            with self.assertRaises(HTTPException) as cm:
                tiles_api._resolve_preview_provider("google_img")
            self.assertEqual(cm.exception.status_code, 403)
        finally:
            settings.google.enabled = orig

    def test_enabled_google_accepted(self):
        from backend.config import settings
        from backend.providers.google import GoogleProvider
        orig = settings.google.enabled
        settings.google.enabled = True
        try:
            p = tiles_api._resolve_preview_provider("google_img")
            self.assertIsInstance(p, GoogleProvider)
        finally:
            settings.google.enabled = orig

    def test_enabled_esri_accepted(self):
        from backend.config import settings
        from backend.providers.esri_imagery import EsriImageryProvider
        orig = settings.esri_imagery.enabled
        settings.esri_imagery.enabled = True
        try:
            p = tiles_api._resolve_preview_provider("esri_imagery")
            self.assertIsInstance(p, EsriImageryProvider)
        finally:
            settings.esri_imagery.enabled = orig


class TestTransparentFallback(unittest.TestCase):
    def test_placeholder_is_valid_png(self):
        png = tiles_api._TRANSPARENT_PNG
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_placeholder_is_tiny(self):
        """1x1 透明 PNG,避免每次失败都传一大坨。"""
        self.assertLess(len(tiles_api._TRANSPARENT_PNG), 200)


class TestLimits(unittest.TestCase):
    def test_preview_concurrency_below_download(self):
        """预览并发必须小于下载并发:争抢代理时该让下载优先。"""
        from backend.config import settings
        self.assertLess(tiles_api._PREVIEW_CONCURRENCY,
                        settings.download.concurrency)

    def test_preview_timeout_much_shorter_than_download(self):
        """预览超时必须远小于下载超时:主进程要服务所有 HTTP 与 WS,
        挂 30s 的转发会拖垮交互 —— 那正是进程隔离要解决的问题。"""
        from backend.config import settings
        self.assertLess(tiles_api._PREVIEW_TIMEOUT, settings.download.timeout)
        self.assertLessEqual(tiles_api._PREVIEW_TIMEOUT, 10)


class TestNoCaching(unittest.TestCase):
    def test_module_does_not_touch_cache_dir(self):
        """预览不写缓存(设计 D6):混用会让下载进度估算失真。"""
        import inspect
        src = inspect.getsource(tiles_api)
        self.assertNotIn("cache_dir", src)
        self.assertNotIn("tile_path", src)


if __name__ == "__main__":
    unittest.main()
