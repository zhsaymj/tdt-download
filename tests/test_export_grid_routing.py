"""导出阶段的网格分流。

只验证"选对了哪条路径/参数",不跑真实切片(那在手工验收里做)。
"""
import unittest

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import _mbtiles_scheme_for, _tms_needs_resample


class TestTmsRouting(unittest.TestCase):
    def test_geodetic_uses_direct_mapping(self):
        """天地图 4326 与 gdal2tiles geodetic 网格同构,无损直映射。"""
        self.assertFalse(_tms_needs_resample(GEO_GEODETIC))

    def test_mercator_needs_resample(self):
        """TMS 是 4326 网格;3857 源必须重采样,不能直映射。"""
        self.assertTrue(_tms_needs_resample(GEO_MERCATOR))


class TestMbtilesScheme(unittest.TestCase):
    def test_geodetic_is_tms_scheme(self):
        self.assertEqual(_mbtiles_scheme_for(GEO_GEODETIC, "tms"), "tms")

    def test_osm_dir_is_always_xyz(self):
        """osm 目录的行号自北向南,恒为 xyz 约定,与源网格无关。"""
        self.assertEqual(_mbtiles_scheme_for(GEO_GEODETIC, "osm"), "xyz")
        self.assertEqual(_mbtiles_scheme_for(GEO_MERCATOR, "osm"), "xyz")

    def test_tms_dir_is_always_tms(self):
        """tms 目录的行号自南向北,恒为 tms 约定。"""
        self.assertEqual(_mbtiles_scheme_for(GEO_MERCATOR, "tms"), "tms")
        self.assertEqual(_mbtiles_scheme_for(GEO_GEODETIC, "tms"), "tms")


if __name__ == "__main__":
    unittest.main()
