"""注记的瓦片预估:按实际会下的级别加增量,不整体翻倍。

`_estimate_detail` 已返回逐级别明细 {z, tiles, bytes},直接筛 ≤18 求和即可
—— 不需要新增均值常量,也不会把"注记有级别上限"这件事漏掉。
"""
import unittest

from backend.api.tasks import _add_annotation_to_detail

BBOX = (116.36, 39.98, 116.41, 40.03)      # 0.05 度


class TestAddAnnotationToDetail(unittest.TestCase):
    """注记增量按 ≤z18 逐级别加,不是整体翻倍。

    原地修改 detail:逐级别与合计一起更新,避免界面表格与合计自相矛盾。
    """

    def _detail(self, levels, provider="google_img"):
        from backend.api.tasks import _estimate_detail
        return _estimate_detail(BBOX, levels, provider)

    def _apply(self, levels, provider="google_img"):
        d = self._detail(levels, provider)
        _add_annotation_to_detail(d, levels)
        return d

    def test_below_18_doubles(self):
        """z16~18 全部 ≤18 -> 每级翻倍。"""
        base = self._detail([16, 17, 18])
        got = self._apply([16, 17, 18])
        self.assertEqual(got["total_tiles"], base["total_tiles"] * 2)

    def test_above_18_adds_nothing(self):
        """★ 核心用例 ★ z19~21 注记一张都不下 -> 完全不变。"""
        base = self._detail([19, 20, 21])
        got = self._apply([19, 20, 21])
        self.assertEqual(got["total_tiles"], base["total_tiles"])
        self.assertEqual(got["total_bytes"], base["total_bytes"])

    def test_mixed_levels_only_double_below_18(self):
        """z17~19 -> 只有 17/18 翻倍,19 不变。"""
        base = self._detail([17, 18, 19])
        got = self._apply([17, 18, 19])
        b = {r["z"]: r["tiles"] for r in base["levels"]}
        g = {r["z"]: r["tiles"] for r in got["levels"]}
        self.assertEqual(g[17], b[17] * 2)
        self.assertEqual(g[18], b[18] * 2)
        self.assertEqual(g[19], b[19])

    def test_boundary_18_doubles_19_not(self):
        base = self._detail([18, 19])
        got = self._apply([18, 19])
        b = {r["z"]: r["tiles"] for r in base["levels"]}
        g = {r["z"]: r["tiles"] for r in got["levels"]}
        self.assertEqual(g[18], b[18] * 2)
        self.assertEqual(g[19], b[19])

    def test_tianditu_all_below_doubles(self):
        """天地图源级别都 ≤18 -> 与旧的 total*=2 等价。"""
        base = self._detail([15, 16, 17, 18], provider="tianditu_img")
        got = self._apply([15, 16, 17, 18], provider="tianditu_img")
        self.assertEqual(got["total_tiles"], base["total_tiles"] * 2)

    def test_total_always_equals_sum_of_rows(self):
        """逐级别之和必须等于 total —— 否则界面表格与合计打架。"""
        for lv in ([15], [15, 16], [18, 19], [19, 20, 21], list(range(1, 22))):
            got = self._apply(lv)
            self.assertEqual(got["total_tiles"],
                             sum(r["tiles"] for r in got["levels"]), lv)
            self.assertEqual(got["total_bytes"],
                             sum(r["bytes"] for r in got["levels"]), lv)

    def test_empty_levels_is_noop(self):
        d = {"levels": [], "total_tiles": 0, "total_bytes": 0}
        _add_annotation_to_detail(d, [15])
        self.assertEqual(d["total_tiles"], 0)

    def test_missing_levels_key_is_safe(self):
        d = {}
        _add_annotation_to_detail(d, [15, 16])   # 不该抛


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



class TestEstimateApiMatchesCreate(unittest.TestCase):
    """★ 核心一致性问题 ★ 预估接口与建任务必须给出同一个数。

    实测发现的 bug:`/api/tasks/estimate` 原先**不接受 annotate 参数**,
    注记增量是前端自己整体 ×2 补的 —— 而整体 ×2 对 z19+ 是错的
    (天地图注记只到 z18)。后果是对话框预估与建成后的任务数对不上:

        levels=14~18  预估 823   任务 1646  (任务翻倍、预估没翻)
        levels=1~21   预估 45267 任务 46106 (差 839)
        levels=19~21  预估 44428 任务 44428 (一致,因为增量为 0)

    现在预估接口自己接受 annotate 并按 ≤z18 逐级别加增量,前端不再乘。
    """

    BBOX = BBOX

    def _estimate(self, levels, provider, annotate):
        from backend.api.tasks import api_estimate
        import asyncio

        async def run():
            return await api_estimate(
                *self.BBOX, levels=",".join(map(str, levels)),
                provider=provider, annotate=annotate)
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run())
        finally:
            loop.close()

    def _create_total(self, levels, provider, annotate):
        from backend.api.tasks import api_create_task
        from backend.db import init_db
        from backend.models import TaskCreate, delete_task
        import asyncio

        async def run():
            init_db()
            d = TaskCreate(name="est_cmp", provider=provider,
                           bbox=list(self.BBOX), levels=list(levels),
                           export="geotiff", crs="EPSG:3857",
                           annotate=annotate)
            r = await api_create_task(d)
            delete_task(r["id"])
            return r["total"]
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(run())
        finally:
            loop.close()

    def test_below_18_agree(self):
        est = self._estimate([14, 15, 16, 17, 18], "google_img", True)
        self.assertEqual(est["total_tiles"],
                         self._create_total([14, 15, 16, 17, 18],
                                            "google_img", True))

    def test_above_18_agree(self):
        est = self._estimate([19, 20, 21], "google_img", True)
        self.assertEqual(est["total_tiles"],
                         self._create_total([19, 20, 21], "google_img", True))

    def test_mixed_agree(self):
        est = self._estimate(list(range(1, 22)), "google_img", True)
        self.assertEqual(est["total_tiles"],
                         self._create_total(list(range(1, 22)),
                                            "google_img", True))

    def test_no_annotate_agree(self):
        est = self._estimate([14, 15], "google_img", False)
        self.assertEqual(est["total_tiles"],
                         self._create_total([14, 15], "google_img", False))

    def test_tianditu_agree(self):
        est = self._estimate([15, 16], "tianditu_img", True)
        self.assertEqual(est["total_tiles"],
                         self._create_total([15, 16], "tianditu_img", True))

    def test_annotate_actually_adds_for_below_18(self):
        """勾注记确实让 ≤18 的级别翻倍(不是没生效)。"""
        off = self._estimate([15, 16], "google_img", False)["total_tiles"]
        on = self._estimate([15, 16], "google_img", True)["total_tiles"]
        self.assertEqual(on, off * 2)

    def test_annotate_adds_nothing_for_above_18(self):
        """★ 核心 ★ z19~21 勾注记不该有任何增量。"""
        off = self._estimate([19, 20, 21], "google_img", False)["total_tiles"]
        on = self._estimate([19, 20, 21], "google_img", True)["total_tiles"]
        self.assertEqual(on, off)

    def test_per_level_rows_consistent_with_total(self):
        """逐级别明细之和必须等于 total(否则界面表格与合计自相矛盾)。"""
        est = self._estimate(list(range(1, 22)), "google_img", True)
        self.assertEqual(est["total_tiles"],
                         sum(r["tiles"] for r in est["levels"]))
        self.assertEqual(est["total_bytes"],
                         sum(r["bytes"] for r in est["levels"]))

if __name__ == "__main__":
    unittest.main()
