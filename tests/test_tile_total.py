"""「该任务要下载多少张瓦片」的唯一判定(需求39)。

现象:进度显示 100% 却还在下载,而且**下载数大于总数**。

根因:分母在**建任务时**算好落库,而运行期实际要下的量更大 —— 天地图
tms+osm 同选时会下两套原生瓦片(`core.formats.download_grids_of`),
补下另一套时 `total` 没跟着变。

修法:`core/tile_estimate.tile_total` 成为唯一判定处,建任务(预估/落库)与
运行期(下载阶段的分母)共用它。本文件钉住它的口径,以及"建任务侧与运行期侧
算出来必须一致"这条不变量。
"""
import unittest

from backend.core.tile_estimate import grids_of, tile_total

#: 用户报的那次任务(天地图影像_20260929_185633)的真实范围与级别
_UI_BBOX = (108.33911733456122, 31.13270847681025,
            108.49643047446654, 31.21999041433692)
_UI_LEVELS = list(range(1, 19))


class TileTotalTest(unittest.TestCase):
    def test_two_grids_sum_both(self):
        """★ 核心 ★ 天地图 tms+osm 同选时,两套网格各算一遍再相加。"""
        both = tile_total("tianditu_img", "geotiff,tms,osm", _UI_BBOX, _UI_LEVELS)
        geo = tile_total("tianditu_img", "geotiff,tms", _UI_BBOX, _UI_LEVELS)
        merc = tile_total("tianditu_img", "osm", _UI_BBOX, _UI_LEVELS)
        self.assertEqual(both, geo + merc)
        self.assertNotEqual(geo, merc, "两套网格的计数应当不同")

    def test_matches_the_reported_case(self):
        """★ 回归 ★ 用户那次任务的真实数字。

        库里的 total 是 19862(= 仅 geodetic 9931 × 2 注记,旧口径落库的),
        而实际下了约 43510 张 —— 所以进度越过 100% 还在跑。
        """
        total = tile_total("tianditu_img", "geotiff,tms,osm", _UI_BBOX,
                           _UI_LEVELS, annotate=True)
        self.assertEqual(total, 43510, "双网格 + 注记的口径")
        self.assertGreater(total, 19862, "必须大于旧口径落库的那个值")

    def test_annotation_doubles_only_le18(self):
        """★ 注记只到 z18 ★ z19+ 不能跟着翻倍(否则分母又虚高)。"""
        both = tile_total("google_img", "osm", _UI_BBOX, [18, 19, 20, 21],
                          annotate=True)
        only18 = tile_total("google_img", "osm", _UI_BBOX, [18], annotate=True)
        rest = tile_total("google_img", "osm", _UI_BBOX, [19, 20, 21],
                          annotate=False)
        self.assertEqual(both, only18 + rest)

    def test_no_annotate_is_plain_sum(self):
        a = tile_total("tianditu_img", "tms", _UI_BBOX, _UI_LEVELS)
        b = tile_total("tianditu_img", "tms", _UI_BBOX, _UI_LEVELS, annotate=False)
        self.assertEqual(a, b)

    def test_single_grid_unchanged_against_old_helper(self):
        """回归:_estimate_total(旧口径,不含注记)与它完全一致。"""
        from backend.api.tasks import _estimate_total
        for fmt in ("tms", "osm", "geotiff"):
            with self.subTest(fmt=fmt):
                self.assertEqual(
                    _estimate_total(_UI_BBOX, _UI_LEVELS, "tianditu_img", [fmt]),
                    tile_total("tianditu_img", [fmt], _UI_BBOX, _UI_LEVELS))

    def test_google_never_doubles_grids(self):
        """Google 只有 3857:勾两个格式也只算一套。"""
        self.assertEqual(
            tile_total("google_img", "tms,osm", _UI_BBOX, [12]),
            tile_total("google_img", "osm", _UI_BBOX, [12]))

    def test_empty_levels_is_zero(self):
        self.assertEqual(tile_total("tianditu_img", "tms", _UI_BBOX, []), 0)


class CreateAndRunAgreeTest(unittest.TestCase):
    """★ 不变量 ★ 建任务侧的 `total` 与运行期重算的分母必须同值。

    这两处曾经各算各的 —— 那正是需求39 的成因。现在共用
    `core.tile_estimate.tile_total`,这里把"明细逐级相加 == total"也钉住,
    免得将来有人只改一处。
    """

    def test_detail_sum_equals_tile_total(self):
        from backend.api.tasks import _add_annotation_to_detail, _estimate_detail
        for provider, export, levels, ann in (
            ("tianditu_img", "geotiff,tms,osm", _UI_LEVELS, True),
            ("tianditu_img", "geotiff,tms", _UI_LEVELS, True),
            ("tianditu_img", "osm", _UI_LEVELS, False),
            ("google_img", "geotiff,osm", [16, 17, 18], True),
        ):
            with self.subTest(provider=provider, export=export, ann=ann):
                detail = _estimate_detail(_UI_BBOX, levels, provider, export)
                if ann:
                    _add_annotation_to_detail(detail, levels)
                self.assertEqual(
                    detail["total_tiles"],
                    tile_total(provider, export, _UI_BBOX, levels,
                               annotate=ann),
                    "明细逐级相加与实际要下载的量对不上 —— 两处口径漂了")

    def test_grids_of_accepts_string_and_list(self):
        self.assertEqual(grids_of("tianditu_img", "geotiff,tms,osm"),
                         grids_of("tianditu_img", ["geotiff", "tms", "osm"]))


if __name__ == "__main__":
    unittest.main()
