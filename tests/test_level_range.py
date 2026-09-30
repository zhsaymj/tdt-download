"""level_range —— 层级→瓦片区间 的唯一判定处(设计 D2/D3)。"""
import unittest

from backend.core.mercator_tiling import mercator_range_for_bbox
from backend.core.tile_range import download_levels, level_range
from backend.core.tiling import range_for_bbox

BBOX = (120.5, 30.5, 121.0, 30.9)  # 上海附近 0.5°×0.4°


class LevelRangeTest(unittest.TestCase):
    def test_disabled_identical_to_range_for_bbox(self):
        """回归护栏:global_max_level=0 时与 range_for_bbox 一致。"""
        for z in (6, 10, 15):
            self.assertEqual(
                level_range(z, "geodetic", BBOX),
                range_for_bbox(*BBOX, z))
        for z in (6, 10, 15):
            self.assertEqual(
                level_range(z, "mercator", BBOX),
                mercator_range_for_bbox(*BBOX, z))

    def test_global_level_is_full_matrix(self):
        """全球段 = 整层:geodetic z5=32×16,mercator z5=32×32。"""
        g = level_range(5, "geodetic", BBOX, global_max_level=5)
        self.assertEqual((g.col_max - g.col_min + 1,
                          g.row_max - g.row_min + 1), (32, 16))
        m = level_range(5, "mercator", BBOX, global_max_level=5)
        self.assertEqual((m.col_max - m.col_min + 1,
                          m.row_max - m.row_min + 1), (32, 32))

    def test_buffer_rings_expand(self):
        base = level_range(12, "geodetic", BBOX)
        exp = level_range(12, "geodetic", BBOX, buffer_rings=2)
        self.assertEqual(exp.col_min, base.col_min - 2)
        self.assertEqual(exp.col_max, base.col_max + 2)
        self.assertEqual(exp.row_min, base.row_min - 2)
        self.assertEqual(exp.row_max, base.row_max + 2)

    def test_buffer_clamps_at_matrix_edge(self):
        """贴北极(接近 row=0)的外扩不能越界。"""
        north = (179.0, 89.0, 179.5, 89.5)
        r = level_range(8, "geodetic", north, buffer_rings=5)
        self.assertGreaterEqual(r.col_min, 0)
        self.assertGreaterEqual(r.row_min, 0)
        self.assertLessEqual(r.row_max, (2 ** 7) - 1)   # geodetic z8 行数 2^7

    def test_download_levels_union(self):
        """用户勾 z18 + 全球 z5 → 下载 1..5 ∪ z18。"""
        self.assertEqual(download_levels([18], 5), [1, 2, 3, 4, 5, 18])
        self.assertEqual(download_levels([1, 3, 5], 5), [1, 2, 3, 4, 5])
        self.assertEqual(download_levels([18], 0), [18])   # 关闭时原样


if __name__ == "__main__":
    unittest.main()