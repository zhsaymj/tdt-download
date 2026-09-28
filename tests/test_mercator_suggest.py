"""墨卡托影像的建议级别,以及 suggest_dem_levels 不回归。

两者共用判据实现但参数不同:
  DEM  : z_floor=0, tile_budget=2000(LERC 解码慢)
  影像 : z_floor=1, tile_budget=8000(jpg 解码快;z0 对影像无意义)
"""
import unittest

from backend.core.mercator_tiling import suggest_mercator_levels

BBOX_SMALL = (116.36, 39.98, 116.41, 40.03)      # 0.05 度,约一个城区
BBOX_BIG = (116.0, 39.5, 117.0, 40.5)            # 1 度


class TestSuggestMercatorLevels(unittest.TestCase):
    def test_returns_expected_keys(self):
        got = suggest_mercator_levels(BBOX_SMALL, 21)
        for k in ("levels", "recommended", "recommended_tiles",
                  "budget_limited", "max_useful", "min_useful"):
            self.assertIn(k, got, k)

    def test_z_floor_is_one_not_zero(self):
        """影像的 z0 单张盖全球,毫无意义。天地图影像的下限就是 1。"""
        zs = [r["z"] for r in suggest_mercator_levels(BBOX_SMALL, 21)["levels"]]
        self.assertEqual(min(zs), 1)
        self.assertNotIn(0, zs)

    def test_recommended_excludes_zero(self):
        rec = suggest_mercator_levels(BBOX_SMALL, 21)["recommended"]
        self.assertNotIn(0, rec)

    def test_respects_max_cap(self):
        zs = [r["z"] for r in suggest_mercator_levels(BBOX_SMALL, 19)["levels"]]
        self.assertEqual(max(zs), 19)

    def test_recommended_within_budget(self):
        got = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=8000)
        self.assertLessEqual(got["recommended_tiles"], 8000)

    def test_recommended_not_topping_out_at_21(self):
        """默认预算下不该推荐到 z21(0.05 度选区 z21 是 11 万张)。"""
        rec = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=8000)["recommended"]
        self.assertTrue(rec)
        self.assertLess(max(rec), 21)

    def test_big_bbox_is_budget_limited(self):
        got = suggest_mercator_levels(BBOX_BIG, 21, tile_budget=8000)
        self.assertTrue(got["budget_limited"])

    def test_custom_budget_changes_recommendation(self):
        lo = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=500)
        hi = suggest_mercator_levels(BBOX_SMALL, 21, tile_budget=50000)
        self.assertLessEqual(max(lo["recommended"]), max(hi["recommended"]))

    def test_level_row_shape(self):
        row = suggest_mercator_levels(BBOX_SMALL, 21)["levels"][0]
        self.assertEqual(set(row), {"z", "tiles", "ratio", "useful"})

    def test_recommended_is_contiguous_ascending(self):
        rec = suggest_mercator_levels(BBOX_SMALL, 21)["recommended"]
        self.assertEqual(rec, sorted(rec))
        self.assertEqual(rec, list(range(rec[0], rec[-1] + 1)))


class TestDemSuggestNotRegressed(unittest.TestCase):
    """回归护栏:提取共用实现后,DEM 版的行为必须逐字段不变。"""

    def test_dem_still_starts_at_zero(self):
        from backend.core.dem_tiling import suggest_dem_levels
        zs = [r["z"] for r in suggest_dem_levels(BBOX_SMALL, 16)["levels"]]
        self.assertEqual(min(zs), 0)

    def test_dem_default_cap_is_16(self):
        from backend.core.dem_tiling import suggest_dem_levels
        zs = [r["z"] for r in suggest_dem_levels(BBOX_SMALL)["levels"]]
        self.assertEqual(max(zs), 16)

    def test_dem_keys_unchanged(self):
        from backend.core.dem_tiling import suggest_dem_levels
        got = suggest_dem_levels(BBOX_SMALL, 16)
        for k in ("levels", "recommended", "recommended_tiles",
                  "budget_limited", "max_useful", "min_useful"):
            self.assertIn(k, got, k)

    def test_dem_level_row_shape_unchanged(self):
        from backend.core.dem_tiling import suggest_dem_levels
        row = suggest_dem_levels(BBOX_SMALL, 16)["levels"][0]
        self.assertEqual(set(row), {"z", "tiles", "ratio", "useful"})

    def test_dem_default_budget_is_2000(self):
        """DEM 的预算仍是 2000(为 LERC 解码慢而定),不能跟着影像改成 8000。"""
        from backend.core.dem_tiling import suggest_dem_levels
        got = suggest_dem_levels(BBOX_SMALL, 16)
        self.assertLessEqual(got["recommended_tiles"], 2000)


if __name__ == "__main__":
    unittest.main()
