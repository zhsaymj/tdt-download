"""Esri World Imagery provider 与占位图判定。

判定测试用的是**实抓的真实瓦片字节**(tests/fixtures/),不是构造的数据。
最重要的一条是 test_real_ocean_tile_not_empty:678 字节的真实深海瓦片
比占位图还小、色值还少,朴素判据会把它误判成"无数据"。
"""
import hashlib
import unittest
from pathlib import Path

from backend.config import EsriImageryConfig
from backend.providers.esri_imagery import (
    PLACEHOLDER_SHA256, build_esri_imagery_provider, is_esri_imagery_provider,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _cfg(**kw) -> EsriImageryConfig:
    base = EsriImageryConfig(enabled=True, proxy="127.0.0.1:6789")
    for k, v in kw.items():
        setattr(base, k, v)
    return base


def _fx(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class TestRegistry(unittest.TestCase):
    def test_is_esri_imagery_provider(self):
        self.assertTrue(is_esri_imagery_provider("esri_imagery"))
        self.assertFalse(is_esri_imagery_provider("esri_terrain"))
        self.assertFalse(is_esri_imagery_provider("google_img"))

    def test_metadata(self):
        p = build_esri_imagery_provider(_cfg())
        self.assertEqual(p.key, "esri_imagery")
        self.assertEqual(p.ext, "jpg")
        self.assertEqual(p.bands, 3)


class TestTileUrl(unittest.TestCase):
    def test_arcgis_order_is_z_y_x(self):
        """ArcGIS 的 URL 顺序是 {z}/{y}/{x} —— 写成 {z}/{x}/{y} 会取到
        转置后的错误瓦片(图像看着像但地理位置错),是最隐蔽的一类 bug。"""
        p = build_esri_imagery_provider(_cfg())
        self.assertTrue(p.tile_url(53955, 24810, 16).endswith("/16/24810/53955"))

    def test_custom_template(self):
        p = build_esri_imagery_provider(
            _cfg(url_template="https://x.example.com/{z}/{y}/{x}.jpg"))
        self.assertEqual(p.tile_url(7, 8, 9), "https://x.example.com/9/8/7.jpg")

    def test_empty_template_rejected(self):
        with self.assertRaises(ValueError):
            build_esri_imagery_provider(_cfg(url_template=""))


class TestZoomAndProxy(unittest.TestCase):
    def test_max_zoom_is_19(self):
        """实测 z19 是亚欧城市上限且载有真实新增细节;z20 仅美国境内有。"""
        self.assertEqual(build_esri_imagery_provider(_cfg()).max_zoom(), 19)

    def test_max_zoom_configurable(self):
        self.assertEqual(
            build_esri_imagery_provider(_cfg(max_zoom=20)).max_zoom(), 20)

    def test_min_zoom_is_one(self):
        self.assertEqual(build_esri_imagery_provider(_cfg()).min_zoom(), 1)

    def test_proxy_normalized(self):
        p = build_esri_imagery_provider(_cfg(proxy="127.0.0.1:6789"))
        self.assertEqual(p.proxy, "http://127.0.0.1:6789")

    def test_no_missing_statuses(self):
        """Esri 用 200 + 占位图表达无数据,不用状态码。"""
        self.assertEqual(build_esri_imagery_provider(_cfg()).missing_statuses(),
                         frozenset())


class TestPlaceholderFingerprint(unittest.TestCase):
    def test_fixture_matches_recorded_sha(self):
        """夹具与代码里的指纹常量必须一致 —— 不一致说明有一方被改过。"""
        data = _fx("esri_wi_placeholder_2521B.jpg")
        self.assertEqual(hashlib.sha256(data).hexdigest(), PLACEHOLDER_SHA256)

    def test_fixture_size_is_2521(self):
        self.assertEqual(len(_fx("esri_wi_placeholder_2521B.jpg")), 2521)


class TestIsEmptyTile(unittest.TestCase):
    def setUp(self):
        self.p = build_esri_imagery_provider(_cfg())

    def test_placeholder_is_empty(self):
        """正样本:占位图必须识别。"""
        self.assertTrue(self.p.is_empty_tile(_fx("esri_wi_placeholder_2521B.jpg")))

    def test_real_ocean_tile_not_empty(self):
        """★ 关键负样本 ★

        678 字节的真实深海瓦片:比占位图更小(678 < 2521)、色值更少
        (11 < 79)。若 is_empty_tile 用"体量小 + 色值少"这类朴素规则,
        这条测试会红 —— 这正是它存在的意义。

        误判的后果是静默的数据空洞:深海区域在成果里变 nodata,
        且瓦片不入缓存,每次重跑都重新下载。
        """
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_ocean_678B.jpg")))

    def test_real_sahara_not_empty(self):
        """负样本:体量小(3578B)且明亮(mean 154.68)的真实瓦片。"""
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_sahara.jpg")))

    def test_real_beijing_not_empty(self):
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_beijing.jpg")))

    def test_real_tibet_not_empty(self):
        self.assertFalse(self.p.is_empty_tile(_fx("esri_wi_real_tibet_z17.jpg")))

    def test_empty_bytes_not_empty_tile(self):
        """空响应不是"占位图",应交给上层按失败处理。"""
        self.assertFalse(self.p.is_empty_tile(b""))

    def test_garbage_not_empty_tile(self):
        """解码失败不应抛异常,也不该判成占位图。"""
        self.assertFalse(self.p.is_empty_tile(b"not an image at all"))

    def test_fallback_catches_uniform_bright_tile(self):
        """兜底启发式:体量落在区间内、极亮、几乎无纹理的图应判为占位。

        用直接构造的统计量验证判据本身(不依赖 JPEG 压缩后的实际体量)。
        """
        from backend.providers import esri_imagery as mod
        orig = mod._mean_std
        mod._mean_std = lambda data: (204.7, 5.4)
        try:
            # 体量落在 2000~3000 且统计量像占位图 -> 判为空
            self.assertTrue(self.p.is_empty_tile(b"x" * 2500))
        finally:
            mod._mean_std = orig

    def test_fallback_rejects_dark_tile_in_size_range(self):
        """体量在区间内但偏暗(像真实深海)不能判为占位。"""
        from backend.providers import esri_imagery as mod
        orig = mod._mean_std
        mod._mean_std = lambda data: (20.0, 15.0)
        try:
            self.assertFalse(self.p.is_empty_tile(b"x" * 2500))
        finally:
            mod._mean_std = orig

    def test_fallback_rejects_textured_bright_tile(self):
        """体量在区间内、亮但有纹理(如雪原/沙漠)不能判为占位。"""
        from backend.providers import esri_imagery as mod
        orig = mod._mean_std
        mod._mean_std = lambda data: (200.0, 28.0)
        try:
            self.assertFalse(self.p.is_empty_tile(b"x" * 2500))
        finally:
            mod._mean_std = orig


if __name__ == "__main__":
    unittest.main()
