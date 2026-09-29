"""估算与建议级别的网格分流。

最重要的一条是 test_huge_range_not_rejected:用户明确要求不设瓦片数上限
(设计 §9 Q1),这条护栏防止后人好心加回 MERCATOR_TILE_LIMIT。
"""
import unittest

from backend.api.tasks import _estimate_detail, _estimate_total
from backend.core.mercator_tiling import estimate_mercator_tiles

BBOX = (116.36, 39.98, 116.41, 40.03)      # 0.05 度


class TestEstimateTotal(unittest.TestCase):
    def test_mercator_image_uses_xyz_count(self):
        got = _estimate_total(BBOX, [18], "google_img")
        self.assertEqual(got, estimate_mercator_tiles(BBOX, [18]))
        self.assertEqual(got, 1862)

    def test_esri_imagery_same_grid_as_google(self):
        """两者网格同构,计数必须一致。"""
        self.assertEqual(_estimate_total(BBOX, [18], "esri_imagery"),
                         _estimate_total(BBOX, [18], "google_img"))

    def test_tianditu_still_uses_4326(self):
        """回归护栏:天地图计数不能被改成墨卡托。"""
        from backend.core.tiling import estimate_levels
        self.assertEqual(_estimate_total(BBOX, [18], "tianditu_img"),
                         estimate_levels(BBOX, [18]))

    def test_dem_unchanged(self):
        self.assertEqual(_estimate_total(BBOX, [16], "esri_terrain"),
                         estimate_mercator_tiles(BBOX, [16]))


class TestEstimateDetail(unittest.TestCase):
    def test_mercator_image_detail_shape(self):
        got = _estimate_detail(BBOX, [17, 18], "google_img")
        self.assertIn("levels", got)
        self.assertIn("total_tiles", got)
        self.assertIn("total_bytes", got)
        self.assertEqual(len(got["levels"]), 2)
        for k in ("z", "tiles", "bytes", "cols", "rows", "width", "height"):
            self.assertIn(k, got["levels"][0], k)

    def test_totals_are_sum_of_levels(self):
        got = _estimate_detail(BBOX, [17, 18], "google_img")
        self.assertEqual(got["total_tiles"],
                         sum(r["tiles"] for r in got["levels"]))

    def test_pixel_dims_match_tile_grid(self):
        got = _estimate_detail(BBOX, [18], "google_img")
        row = got["levels"][0]
        self.assertEqual(row["width"], row["cols"] * 256)
        self.assertEqual(row["height"], row["rows"] * 256)

    def test_image_avg_bytes_smaller_than_dem(self):
        """影像 jpg 比 DEM 的 LERC 小得多,估算体积不该套用 DEM 的经验值。"""
        img = _estimate_detail(BBOX, [16], "google_img")
        dem = _estimate_detail(BBOX, [16], "esri_terrain")
        self.assertLess(img["total_bytes"], dem["total_bytes"])

    def test_tianditu_unchanged(self):
        """回归护栏:天地图仍走 4326 明细。"""
        from backend.core.tiling import estimate_levels_detail
        got = _estimate_detail(BBOX, [15], "tianditu_img")
        self.assertEqual(got, estimate_levels_detail(BBOX, [15], "tianditu_img"))


class TestNoTileLimit(unittest.TestCase):
    """★ 回归护栏 ★ 用户明确要求不设单任务瓦片数上限(设计 §9 Q1)。

    实现时**不要**加回 MERCATOR_TILE_LIMIT 这类拦截。规模由预估如实
    呈现、由用户判断;要限制的话该做的是任务分块或调度优先级,不是拒绝提交。
    """

    def test_huge_range_not_rejected(self):
        """3 度选区 z21 约 4 亿张:必须正常返回预估,不抛异常。"""
        big = (115.0, 38.5, 118.0, 41.5)
        got = _estimate_total(big, [21], "google_img")
        self.assertGreater(got, 300_000_000)

    def test_huge_range_detail_not_rejected(self):
        big = (115.0, 38.5, 118.0, 41.5)
        got = _estimate_detail(big, [20, 21], "google_img")
        self.assertGreater(got["total_tiles"], 400_000_000)
        self.assertGreater(got["total_bytes"], 0)

    def test_no_limit_constant_exists(self):
        """确认代码里没有这个常量。"""
        import backend.api.tasks as mod
        self.assertFalse(hasattr(mod, "MERCATOR_TILE_LIMIT"))
        import backend.core.mercator_tiling as mt
        self.assertFalse(hasattr(mt, "MERCATOR_TILE_LIMIT"))


class TestLevelBounds(unittest.TestCase):
    def test_z_cap_per_provider(self):
        from backend.api.tasks import _z_cap_for
        self.assertEqual(_z_cap_for("tianditu_img"), 18)
        self.assertEqual(_z_cap_for("google_img"), 21)
        self.assertEqual(_z_cap_for("esri_imagery"), 19)
        self.assertEqual(_z_cap_for("esri_terrain"), 16)

    def test_z_floor_per_provider(self):
        from backend.api.tasks import _z_floor_for
        self.assertEqual(_z_floor_for("esri_terrain"), 0)      # DEM 从 0 起
        self.assertEqual(_z_floor_for("google_img"), 1)        # 影像从 1 起
        self.assertEqual(_z_floor_for("esri_imagery"), 1)
        self.assertEqual(_z_floor_for("tianditu_img"), 1)


if __name__ == "__main__":
    unittest.main()
