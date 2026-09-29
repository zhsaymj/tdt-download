"""预估要按**实际要下载的网格**计数(设计 D5 / Task 5)。

天地图同时勾 tms+osm 时会下两套原生瓦片,而两套的瓦片数**不同**
(geodetic 第 z 级是 2^z×2^(z-1),mercator 是 2^z×2^z)—— 不是简单翻倍,
必须各算一遍再相加。

预估是用户提交前**唯一能看到规模**的途径(本项目不设单任务瓦片数上限),
少算一半会让用户以为实际下载量出了 bug。
"""
import unittest

from backend.api.tasks import _estimate_total
from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR

_BBOX = (114.2, 30.4, 114.6, 30.8)
_LEVELS = [8, 9]


class EstimateTotalMultiGridTest(unittest.TestCase):
    def test_two_grids_sum_both(self):
        """★ 核心 ★ 天地图 tms+osm → 两套各算一遍再相加。"""
        both = _estimate_total(_BBOX, _LEVELS, "tianditu_img", ["tms", "osm"])
        geo_only = _estimate_total(_BBOX, _LEVELS, "tianditu_img", ["tms"])
        merc_only = _estimate_total(_BBOX, _LEVELS, "tianditu_img", ["osm"])
        self.assertEqual(both, geo_only + merc_only)
        self.assertNotEqual(geo_only, merc_only,
                            "两套网格的瓦片数应当不同,否则这条测试没有意义")

    def test_tms_only_unchanged(self):
        """回归:只勾 tms 时与改动前完全一致(天地图主网格就是 geodetic)。"""
        from backend.core.tiling import estimate_levels
        self.assertEqual(_estimate_total(_BBOX, _LEVELS, "tianditu_img", ["tms"]),
                         estimate_levels(_BBOX, _LEVELS))

    def test_osm_only_switches_to_mercator_grid(self):
        """★ 有意改变 ★ 天地图只勾 osm 时,不再下 geodetic 那套 —— 改下 `_w`。

        这正是本设计的目的:让 osm 拿到原生 3857 瓦片、不做重投影。
        所以计数**应当**与改动前(按 geodetic 算)不同。
        """
        from backend.api.tasks import _estimate_total as et
        got = et(_BBOX, _LEVELS, "tianditu_img", ["osm"])
        self.assertEqual(got, et(_BBOX, _LEVELS, "google_img"),
                         "天地图 osm 的计数口径应与任何 3857 源一致")
        self.assertNotEqual(got, et(_BBOX, _LEVELS, "tianditu_img"),
                            "仍按 geodetic 计的话说明没切到 _w 网格")

    def test_google_never_doubles(self):
        """Google 只有 3857:勾两个格式也只算一套,不凭空翻倍。"""
        self.assertEqual(
            _estimate_total(_BBOX, _LEVELS, "google_img", ["tms", "osm"]),
            _estimate_total(_BBOX, _LEVELS, "google_img"))

    def test_empty_formats_keeps_default(self):
        self.assertEqual(_estimate_total(_BBOX, _LEVELS, "tianditu_img", []),
                         _estimate_total(_BBOX, _LEVELS, "tianditu_img"))


class EstimateEndpointTest(unittest.TestCase):
    def test_endpoint_reports_grids_and_double_total(self):
        """接口层要如实报出"按两套网格计",前端才提示得出来。"""
        import asyncio
        from backend.api.tasks import api_estimate
        one = asyncio.run(api_estimate(*_BBOX, levels="8,9",
                                       provider="tianditu_img", export="tms"))
        two = asyncio.run(api_estimate(*_BBOX, levels="8,9",
                                       provider="tianditu_img",
                                       export="tms,osm"))
        self.assertEqual(one["grids"], [GEO_GEODETIC])
        self.assertEqual(two["grids"], [GEO_GEODETIC, GEO_MERCATOR])
        self.assertEqual(two["total"], one["total"] + _estimate_total(
            _BBOX, _LEVELS, "tianditu_img", ["osm"]))

    def test_endpoint_single_grid_unaffected(self):
        """回归:不传 export 时与改动前一致(旧前端不会传)。"""
        import asyncio
        from backend.api.tasks import api_estimate
        got = asyncio.run(api_estimate(*_BBOX, levels="8,9",
                                       provider="tianditu_img"))
        self.assertEqual(got["grids"], [GEO_GEODETIC])
        self.assertEqual(got["total"],
                         _estimate_total(_BBOX, _LEVELS, "tianditu_img"))


if __name__ == "__main__":
    unittest.main()
