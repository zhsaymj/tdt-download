"""全球底图段的层级必须完整(从 z1 起),不受局部段的层级下限影响。

实测踩到:OSM 全球段输出的是 z[3,4,5] —— 缺了 z1/z2,因为实现里套用了
OSM_MIN_LEVEL(=3)。那个常量是给**局部段**定的(小范围在超低层基本全透明,
切了没意义),但全球段缩到最顶层时正需要 z1/z2,少了它们客户端缩远了显示空白。
"""
import unittest

from backend.core.osm_cache import global_osm_levels
from backend.core.tile_range import download_levels


class GlobalOsmLevelsTest(unittest.TestCase):
    def test_starts_from_one(self):
        """★ 核心 ★ 全球段从 z1 起,不能套用 OSM_MIN_LEVEL。"""
        lv = global_osm_levels(5)
        self.assertEqual(lv, [1, 2, 3, 4, 5])
        self.assertIn(1, lv, "缺 z1 —— 缩到全球最顶层会显示空白")
        self.assertIn(2, lv, "缺 z2")

    def test_respects_upper_bound(self):
        self.assertEqual(global_osm_levels(3), [1, 2, 3])
        self.assertEqual(global_osm_levels(0), [])


class TmsExportLevelsTest(unittest.TestCase):
    """TMS 侧同理:导出层级必须含全球段,即使用户只勾了高层级。"""

    def test_export_levels_include_global_segment(self):
        # 用户只勾 z10-12,但全球段铺到 z5 → 导出层级要有 1..5
        got = download_levels([10, 11, 12], 5)
        self.assertEqual(got, [1, 2, 3, 4, 5, 10, 11, 12])

    def test_disabled_keeps_user_levels(self):
        self.assertEqual(download_levels([10, 12], 0), [10, 12])


if __name__ == "__main__":
    unittest.main()
