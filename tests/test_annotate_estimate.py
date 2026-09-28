"""注记的瓦片预估:按实际会下的级别加增量,不整体翻倍。

`_estimate_detail` 已返回逐级别明细 {z, tiles, bytes},直接筛 ≤18 求和即可
—— 不需要新增均值常量,也不会把"注记有级别上限"这件事漏掉。
"""
import unittest

from backend.api.tasks import _anno_extra

BBOX = (116.36, 39.98, 116.41, 40.03)      # 0.05 度


class TestAnnoExtra(unittest.TestCase):
    def _detail(self, levels, provider="google_img"):
        from backend.api.tasks import _estimate_detail
        return _estimate_detail(BBOX, levels, provider)

    def test_google_below_18_doubles(self):
        """z16~18 全部 ≤18 → 增量 = 底图瓦片数(等价于翻倍)。"""
        d = self._detail([16, 17, 18])
        tiles, bts = _anno_extra(d, [16, 17, 18])
        self.assertEqual(tiles, d["total_tiles"])
        self.assertEqual(bts, d["total_bytes"])

    def test_google_above_18_adds_nothing(self):
        """★ 核心用例 ★ z19~21 注记一张都不下 → 增量为 0,不是翻倍。"""
        d = self._detail([19, 20, 21])
        tiles, bts = _anno_extra(d, [19, 20, 21])
        self.assertEqual(tiles, 0)
        self.assertEqual(bts, 0)

    def test_mixed_levels_only_count_below_18(self):
        """z17~19 → 只加 z17/z18 的部分。"""
        d = self._detail([17, 18, 19])
        tiles, _bts = _anno_extra(d, [17, 18, 19])
        below = sum(r["tiles"] for r in d["levels"] if r["z"] <= 18)
        self.assertEqual(tiles, below)
        self.assertLess(tiles, d["total_tiles"])   # 不是整体翻倍

    def test_boundary(self):
        d = self._detail([18, 19])
        tiles, _ = _anno_extra(d, [18, 19])
        only18 = next(r["tiles"] for r in d["levels"] if r["z"] == 18)
        self.assertEqual(tiles, only18)

    def test_tianditu_all_below_doubles(self):
        """天地图源级别都 ≤18 → 与旧的 total*=2 等价(行为不变)。"""
        d = self._detail([15, 16, 17, 18], provider="tianditu_img")
        tiles, _ = _anno_extra(d, [15, 16, 17, 18])
        self.assertEqual(tiles, d["total_tiles"])

    def test_tianditu_above_18_impossible_but_safe(self):
        """天地图最高 18,但函数本身对超限级别同样安全。"""
        d = self._detail([15, 16, 17, 18], provider="tianditu_img")
        tiles, _ = _anno_extra(d, [19, 20])
        self.assertEqual(tiles, 0)

    def test_empty_levels(self):
        d = self._detail([15])
        self.assertEqual(_anno_extra(d, []), (0, 0))

    def test_missing_levels_key_is_safe(self):
        """detail 结构异常时不抛异常(防御:回归别处调用)。"""
        self.assertEqual(_anno_extra({}, [15, 16]), (0, 0))


class TestNoDoubleMultiplyLeft(unittest.TestCase):
    """回归护栏:api/tasks.py 里不该再有整体翻倍。"""

    def test_no_blind_double(self):
        import inspect
        import backend.api.tasks as mod
        src = inspect.getsource(mod.api_create_task).replace(" ", "")
        self.assertNotIn("total*=2", src,
                         "注记不能再整体翻倍 —— 对 z19+ 会虚高一倍")
        self.assertNotIn("est_bytes*=2", src,
                         "注记不能再整体翻倍 —— 对 z19+ 会虚高一倍")


if __name__ == "__main__":
    unittest.main()
