"""天地图注记 provider 的矩阵集支持。

关键护栏:默认 matrix_set="c" 时 URL 必须与改动前逐字符一致 ——
天地图源的下载行为不能因为这次改动而变化。
"""
import unittest

from backend.providers.tianditu import TiandituProvider


class TestMatrixSet(unittest.TestCase):
    def test_default_is_c(self):
        p = TiandituProvider("tianditu_cia", "tk")
        self.assertEqual(p.matrix_set, "c")

    def test_default_url_unchanged(self):
        """★ 回归护栏 ★ 默认行为逐字符不变(天地图源下载不受影响)。"""
        p = TiandituProvider("tianditu_cia", "tk")
        url = p.tile_url(53955, 9102, 16)
        self.assertIn("/cia_c/wmts?", url)
        self.assertIn("TILEMATRIXSET=c", url)
        self.assertIn("TILEROW=9102", url)
        self.assertIn("TILECOL=53955", url)

    def test_w_matrix_set_switches_suffix_and_param(self):
        """matrix_set="w" 时图层后缀与 TILEMATRIXSET 都要变成 w。"""
        p = TiandituProvider("tianditu_cia", "tk", matrix_set="w")
        url = p.tile_url(53955, 24810, 16)
        self.assertIn("/cia_w/wmts?", url)
        self.assertIn("TILEMATRIXSET=w", url)
        self.assertNotIn("TILEMATRIXSET=c", url)

    def test_w_keeps_layer_name_without_suffix(self):
        """LAYER 参数不带矩阵集后缀(是 "cia" 不是 "cia_w")。"""
        p = TiandituProvider("tianditu_cia", "tk", matrix_set="w")
        self.assertIn("LAYER=cia&", p.tile_url(1, 1, 16))

    def test_w_applies_to_all_annotation_layers(self):
        for key, base in (("tianditu_cia", "cia"), ("tianditu_cva", "cva"),
                          ("tianditu_cta", "cta")):
            p = TiandituProvider(key, "tk", matrix_set="w")
            url = p.tile_url(1, 1, 16)
            self.assertIn(f"/{base}_w/wmts?", url, key)
            self.assertIn(f"LAYER={base}&", url, key)

    def test_imagery_layer_also_supports_w(self):
        """底图图层同样适用(不是只有注记能切)。"""
        p = TiandituProvider("tianditu_img", "tk", matrix_set="w")
        url = p.tile_url(1, 1, 16)
        self.assertIn("/img_w/wmts?", url)
        self.assertIn("TILEMATRIXSET=w", url)

    def test_matrix_set_does_not_change_ext_or_bands(self):
        """切矩阵集不该影响瓦片格式与波段数(注记是 4 波段带透明 PNG)。"""
        c = TiandituProvider("tianditu_cia", "tk")
        w = TiandituProvider("tianditu_cia", "tk", matrix_set="w")
        self.assertEqual(c.ext, w.ext)
        self.assertEqual(c.bands, w.bands)

    def test_token_still_appended(self):
        p = TiandituProvider("tianditu_cia", "tk-abc", matrix_set="w")
        self.assertIn("tk=tk-abc", p.tile_url(1, 1, 16))

    def test_unknown_matrix_set_rejected(self):
        """未知矩阵集直接报错,不要静默拼出错 URL。"""
        with self.assertRaises(ValueError):
            TiandituProvider("tianditu_cia", "tk", matrix_set="x")


if __name__ == "__main__":
    unittest.main()
