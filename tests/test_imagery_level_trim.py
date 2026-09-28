"""Esri 区域级别剔除。

关键取舍:**剔除而非拒绝**。用户在西藏选了 z15-z18,z18 无数据时,
下 z15-z17 是合理的期望,不该整个任务被拒。
"""
import unittest

from backend.api.tasks import _trim_levels_for_region


class TestTrimLevels(unittest.TestCase):
    def test_trims_above_probed_max(self):
        kept, dropped = _trim_levels_for_region([15, 16, 17, 18], 17)
        self.assertEqual(kept, [15, 16, 17])
        self.assertEqual(dropped, [18])

    def test_nothing_dropped_when_all_available(self):
        kept, dropped = _trim_levels_for_region([15, 16, 17], 19)
        self.assertEqual(kept, [15, 16, 17])
        self.assertEqual(dropped, [])

    def test_none_probe_keeps_all(self):
        """判不了(网络问题)时放行全部,不能误判成"该范围没影像"而砍级别。"""
        kept, dropped = _trim_levels_for_region([15, 16, 17, 18, 19], None)
        self.assertEqual(kept, [15, 16, 17, 18, 19])
        self.assertEqual(dropped, [])

    def test_never_returns_empty(self):
        """全部超限时保留最低一级,避免产出一个零级别的空任务。"""
        kept, dropped = _trim_levels_for_region([18, 19], 17)
        self.assertEqual(kept, [18])
        self.assertEqual(dropped, [19])

    def test_preserves_order_and_dedups(self):
        kept, _dropped = _trim_levels_for_region([17, 15, 16, 15], 19)
        self.assertEqual(kept, [15, 16, 17])

    def test_empty_input(self):
        kept, dropped = _trim_levels_for_region([], 18)
        self.assertEqual(kept, [])
        self.assertEqual(dropped, [])

    def test_exact_boundary_kept(self):
        """恰好等于探测值要保留(z18 <= z18)。"""
        kept, dropped = _trim_levels_for_region([17, 18], 18)
        self.assertEqual(kept, [17, 18])
        self.assertEqual(dropped, [])


class TestProbeImageryMaxLevel(unittest.TestCase):
    def test_google_does_not_probe(self):
        """Google 实测无地区性降级(陆地处处可到 z21),不该白跑网络请求。"""
        import asyncio
        from backend.api.tasks import _probe_imagery_max_level

        async def run():
            return await _probe_imagery_max_level(
                (116.38, 39.99, 116.39, 40.00), "google_img")

        loop = asyncio.new_event_loop()
        try:
            self.assertIsNone(loop.run_until_complete(run()))
        finally:
            loop.close()

    def test_tianditu_does_not_probe(self):
        import asyncio
        from backend.api.tasks import _probe_imagery_max_level

        async def run():
            return await _probe_imagery_max_level(
                (116.38, 39.99, 116.39, 40.00), "tianditu_img")

        loop = asyncio.new_event_loop()
        try:
            self.assertIsNone(loop.run_until_complete(run()))
        finally:
            loop.close()

    def test_disabled_probe_returns_none(self):
        """probe_max_zoom 关掉时不探测(直接返回 None = 不限制)。"""
        import asyncio
        from backend.api import tasks as mod

        orig = mod.settings.esri_imagery.probe_max_zoom
        mod.settings.esri_imagery.probe_max_zoom = False

        async def run():
            return await mod._probe_imagery_max_level(
                (116.38, 39.99, 116.39, 40.00), "esri_imagery")

        loop = asyncio.new_event_loop()
        try:
            self.assertIsNone(loop.run_until_complete(run()))
        finally:
            loop.close()
            mod.settings.esri_imagery.probe_max_zoom = orig


if __name__ == "__main__":
    unittest.main()
