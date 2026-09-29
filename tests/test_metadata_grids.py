"""元数据里的逐级区间要按**实际下载的网格**描述(最终审查 Important 3)。

原实现恒用 `range_for_bbox`(4326),于是:

* 天地图 `tms,osm`(两套都下):`per_level` 只描述了一套,与顶层 `tile_grids`
  自相矛盾
* 天地图**仅 osm**:`tile_grids` 是 mercator、`tile_matrix_set` 是 XYZ,而
  `per_level` 报的是 geodetic 的行列号 —— 正是设计 §4"任一处都不得按网格 A 的
  行列号描述网格 B"的同形问题(只影响元数据描述,不影响像素,但会误导拿它核对
  覆盖范围的人)
"""
import unittest

from backend.core.metadata import per_level_ranges

_BBOX = (114.2, 30.4, 114.6, 30.8)
_LEVELS = [8, 9]


class PerLevelRangesTest(unittest.TestCase):
    def test_two_grids_each_get_a_segment(self):
        """★ 核心 ★ 两套都下时,逐级区间要两套各出一段并标 grid。"""
        rows = per_level_ranges("tianditu_img", ["tms", "osm"], _LEVELS, _BBOX)
        self.assertEqual({r["grid"] for r in rows}, {"geodetic", "mercator"})
        self.assertEqual(len(rows), len(_LEVELS) * 2, "每级每套各一行")

    def test_mercator_rows_differ_from_geodetic(self):
        """两套的行号必须不同 —— 否则"按哪套描述"这件事看不出来。"""
        rows = per_level_ranges("tianditu_img", ["tms", "osm"], [8], _BBOX)
        g = next(r for r in rows if r["grid"] == "geodetic")
        m = next(r for r in rows if r["grid"] == "mercator")
        self.assertNotEqual((g["row_min"], g["row_max"]),
                            (m["row_min"], m["row_max"]),
                            "同级的行区间相同 → 说明没按各自的网格算")
        self.assertNotEqual(g["tile_count"], m["tile_count"])

    def test_osm_only_describes_mercator(self):
        """★ 仅 osm 时不能再用 geodetic 的行列号描述成果。"""
        rows = per_level_ranges("tianditu_img", ["osm"], [8], _BBOX)
        self.assertEqual({r["grid"] for r in rows}, {"mercator"})
        from backend.core.metadata import per_level_ranges as f
        g = f("tianditu_img", ["tms"], [8], _BBOX)[0]
        self.assertNotEqual(rows[0]["row_min"], g["row_min"],
                            "仅 osm 的行号不该与 geodetic 相同")

    def test_tms_only_unchanged(self):
        """回归:仅 tms(天地图默认)仍是 geodetic、且与改动前同值。"""
        from backend.core.tiling import range_for_bbox
        tr = range_for_bbox(*_BBOX, 8)
        rows = per_level_ranges("tianditu_img", ["tms"], [8], _BBOX)
        self.assertEqual(rows[0]["grid"], "geodetic")
        self.assertEqual(rows[0]["tile_count"], tr.count)
        self.assertEqual(rows[0]["row_min"], tr.row_min)

    def test_google_stays_single_mercator_segment(self):
        """Google 只有 3857:勾两个格式也只出一段,不凭空多一套。"""
        rows = per_level_ranges("google_img", ["tms", "osm"], _LEVELS, _BBOX)
        self.assertEqual({r["grid"] for r in rows}, {"mercator"})
        self.assertEqual(len(rows), len(_LEVELS))

    def test_dem_keeps_mercator(self):
        """DEM 保留原口径(本地 DEM 不在 PROVIDER_GRIDS 里,按源推断会变 geodetic)。"""
        rows = per_level_ranges("local_dem", ["geotiff"], [8], _BBOX, dem=True)
        self.assertEqual({r["grid"] for r in rows}, {"mercator"})


if __name__ == "__main__":
    unittest.main()
