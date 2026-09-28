"""runner 对注记的接入:网格、守卫、z18 裁剪。

不跑真实下载,只验证"选对了 provider 与级别"。
"""
import unittest

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import ANNOTATION_MAX_Z, _anno_levels_for


class TestAnnotationMaxZ(unittest.TestCase):
    def test_constant_is_18(self):
        """实测天地图注记最高 z18(z19+ 返回 213B 空图)。"""
        self.assertEqual(ANNOTATION_MAX_Z, 18)


class TestAnnoLevelsFor(unittest.TestCase):
    def test_filters_above_18(self):
        self.assertEqual(_anno_levels_for([15, 16, 17, 18, 19, 20, 21]),
                         [15, 16, 17, 18])

    def test_all_below_kept(self):
        self.assertEqual(_anno_levels_for([10, 12, 15]), [10, 12, 15])

    def test_all_above_is_empty(self):
        """全超限时注记一张都不下 —— 这正是"估算不能整体翻倍"的由来。"""
        self.assertEqual(_anno_levels_for([19, 20, 21]), [])

    def test_empty_input(self):
        self.assertEqual(_anno_levels_for([]), [])

    def test_boundary_18_kept_19_dropped(self):
        self.assertEqual(_anno_levels_for([18, 19]), [18])

    def test_dedups_and_sorts(self):
        self.assertEqual(_anno_levels_for([17, 15, 16, 15]), [15, 16, 17])


class TestMercatorAnnotationSupported(unittest.TestCase):
    """回归护栏:上一轮加的"墨卡托源无注记"守卫必须已被移除。"""

    def test_annotation_provider_available_for_google(self):
        from backend.providers.tianditu import build_annotation_provider
        p = build_annotation_provider("google_img", "tk", grid=GEO_MERCATOR)
        self.assertIsNotNone(p, "Google 源应能构造注记 provider")
        self.assertEqual(p.matrix_set, "w")

    def test_tianditu_still_works(self):
        from backend.providers.tianditu import build_annotation_provider
        p = build_annotation_provider("tianditu_img", "tk",
                                      grid=GEO_GEODETIC)
        self.assertEqual(p.matrix_set, "c")


class TestRunnerAnnotateGuardRemoved(unittest.TestCase):
    """★ 回归护栏 ★ runner 里不得再有"墨卡托源无注记"的判定。

    该判定是上一轮的临时补丁(当时后端不支持)。现在支持了,
    留着会让 Google/Esri 勾了注记却静默不下载。
    """

    def test_no_mercator_guard_in_source(self):
        import inspect
        import backend.core.runner as mod
        src = inspect.getsource(mod.run_task)
        # 守卫的写法是 annotate = (...) and grid_of(...) != GEO_MERCATOR
        self.assertNotIn("grid_of(task[\"provider\"]) != GEO_MERCATOR", src,
                         "墨卡托守卫仍在 —— Google/Esri 会被静默跳过")

    def test_annotation_uses_grid_aware_construction(self):
        """注记 provider 必须按源网格构造(传 grid= 参数)。"""
        import inspect
        import backend.core.runner as mod
        src = inspect.getsource(mod.run_task)
        self.assertIn("grid=grid_of(task[\"provider\"])", src,
                      "注记 provider 未按源网格构造,会请求到另一个地方的注记")


if __name__ == "__main__":
    unittest.main()
