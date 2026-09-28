"""导出阶段的网格分流。

只验证"选对了哪条路径/参数",不跑真实切片(那在手工验收里做)。
"""
import unittest
from pathlib import Path

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


class TestEmptyOutputGuard(unittest.TestCase):
    """★ 回归护栏 ★ 阶段产出为空必须报错,不能静默成功。

    实测踩到的坑:墨卡托源(3857)曾被直接喂给 export_tms_from_source,
    该函数用 4326 网格枚举输出瓦片再与源图 bounds 求交,源是米制时交集
    恒为空 —— 一张都没切出来,阶段却报 "8/8 张"、状态 done,成果目录全空。
    用户要到打开目录才发现,属于最难排查的一类问题。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_count_tiles_empty_dir(self):
        from backend.core.runner import _count_tiles
        self.assertEqual(_count_tiles(self.dir), 0)

    def test_count_tiles_missing_dir(self):
        from backend.core.runner import _count_tiles
        self.assertEqual(_count_tiles(self.dir / "nope"), 0)

    def test_count_tiles_counts_files_recursively(self):
        from backend.core.runner import _count_tiles
        (self.dir / "14" / "100").mkdir(parents=True)
        (self.dir / "14" / "100" / "200.png").write_bytes(b"x")
        (self.dir / "14" / "100" / "201.png").write_bytes(b"x")
        self.assertEqual(_count_tiles(self.dir), 2)

    def test_guard_flags_when_nothing_produced(self):
        """计划里有瓦片但一个文件都没新增 -> 判为失败。"""
        from backend.core.runner import _tms_output_looks_empty
        self.assertTrue(_tms_output_looks_empty(expected=8, after=0))

    def test_guard_passes_when_tiles_produced(self):
        from backend.core.runner import _tms_output_looks_empty
        self.assertFalse(_tms_output_looks_empty(expected=8, after=8))

    def test_guard_passes_on_resume_adding_nothing_but_dir_populated(self):
        """断点续切:本次没新增但目录已有瓦片,不算失败。"""
        from backend.core.runner import _tms_output_looks_empty
        self.assertFalse(_tms_output_looks_empty(expected=8, after=8))

    def test_guard_ignores_zero_expected(self):
        """计划本就无瓦片(如全在选区外)时不该报错。"""
        from backend.core.runner import _tms_output_looks_empty
        self.assertFalse(_tms_output_looks_empty(expected=0, after=0))


if __name__ == "__main__":
    unittest.main()
