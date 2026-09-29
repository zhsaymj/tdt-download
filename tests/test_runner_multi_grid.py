"""按网格下载(设计 D3 / Task 3)。

天地图两套网格(`_c` geodetic / `_w` mercator)的行号语义不同:第 z 级行数分别是
`2^(z-1)` 与 `2^z`,**同一个 `(col,row)` 不是同一地点**。所以每个网格必须配

* 自己的 provider(天地图靠 `matrix_set` 切换)
* 自己的区间函数(`range_for_bbox` vs `mercator_range_for_bbox`)
* 自己的下载器(缓存路径带网格后缀)
* 自己的注记 provider 与下载器

混用会按错误网格取瓦片,下出来的图**整体错位**(不报错)。
"""
import unittest
from unittest import mock

from backend.core.formats import GEO_GEODETIC, GEO_MERCATOR
from backend.core.runner import (
    _build_provider_for, _download_all_grids, _range_fn_for,
)

_BBOX = (114.2, 30.4, 114.6, 30.8)


class RangeFnGridTest(unittest.TestCase):
    def test_explicit_grid_overrides_provider(self):
        """★ 核心 ★ 显式网格优先 —— 天地图也能拿 mercator 的区间函数。"""
        from backend.core.mercator_tiling import mercator_range_for_bbox
        from backend.core.tiling import range_for_bbox
        self.assertIs(_range_fn_for("tianditu_img", GEO_MERCATOR),
                      mercator_range_for_bbox)
        self.assertIs(_range_fn_for("tianditu_img", GEO_GEODETIC),
                      range_for_bbox)

    def test_provider_inferred_when_grid_absent(self):
        """回归:不传网格时行为与改动前一致(现有调用点都只传 provider)。"""
        from backend.core.mercator_tiling import mercator_range_for_bbox
        from backend.core.tiling import range_for_bbox
        self.assertIs(_range_fn_for("google_img"), mercator_range_for_bbox)
        self.assertIs(_range_fn_for("tianditu_img"), range_for_bbox)

    def test_two_grids_give_disjoint_rows(self):
        """★ 防静默错乱 ★ 同一 bbox/z,两套网格的行号必须不同(否则混用看不出来)。"""
        g = _range_fn_for("tianditu_img", GEO_GEODETIC)(*_BBOX, 6)
        m = _range_fn_for("tianditu_img", GEO_MERCATOR)(*_BBOX, 6)
        self.assertTrue(m.row_max < g.row_min or g.row_max < m.row_min,
                        f"行号范围重叠了,混用不会立即暴露:{g.row_min}..{g.row_max}"
                        f" vs {m.row_min}..{m.row_max}")


class BuildProviderGridTest(unittest.TestCase):
    def test_tianditu_mercator_grid_uses_w(self):
        """★ 核心 ★ 指定 mercator 网格时,天地图 provider 应切到 `_w`。"""
        import backend.core.runner as mod
        orig = mod.settings.tianditu.token
        mod.settings.tianditu.token = "dummy-token"
        try:
            p = _build_provider_for({"provider": "tianditu_img"},
                                    grid=GEO_MERCATOR)
        finally:
            mod.settings.tianditu.token = orig
        self.assertEqual(p.matrix_set, "w")
        self.assertEqual(p.key, "tianditu_img_w",
                         "key 要带网格后缀,否则两套网格的缓存会互相覆盖")

    def test_default_grid_unchanged(self):
        """不传网格时行为与改动前一致。"""
        import backend.core.runner as mod
        orig = mod.settings.tianditu.token
        mod.settings.tianditu.token = "dummy-token"
        try:
            p = _build_provider_for({"provider": "tianditu_img"})
        finally:
            mod.settings.tianditu.token = orig
        self.assertEqual(p.matrix_set, "c")
        self.assertEqual(p.key, "tianditu_img_c")


class _FakeDownloader:
    """记录收到哪些区间,不联网。"""

    def __init__(self, name):
        self.name = name
        self.ranges = []

    async def download_range(self, tr, on_progress, should_stop):
        self.ranges.append((tr.col_min, tr.row_min, tr.z))
        return 1, 0, False


class _StopAfterFirst:
    def __init__(self):
        self.calls = 0

    async def download_range(self, tr, on_progress, should_stop):
        self.calls += 1
        return 1, 0, True


class DownloadAllGridsTest(unittest.TestCase):
    """按网格逐级下载:每个网格用自己的区间函数与下载器。"""

    def _run(self, grids, **kw):
        import asyncio
        made = {}

        def dl_for(grid):
            made.setdefault(grid, _FakeDownloader(grid))
            return made[grid]

        coro = _download_all_grids(
            provider_key="tianditu_img", grids=grids, levels=[5, 6],
            bbox=_BBOX, anno_levels=[5, 6],
            downloader_for=dl_for, anno_for=None,
            on_progress=lambda *a: None, should_stop=lambda: False, **kw)
        stopped = asyncio.run(coro)
        return stopped, made

    def test_single_grid_unchanged(self):
        stopped, made = self._run([GEO_GEODETIC])
        self.assertFalse(stopped)
        self.assertEqual(sorted(made), [GEO_GEODETIC])
        self.assertEqual(len(made[GEO_GEODETIC].ranges), 2, "两个级别各一次")

    def test_both_grids_each_get_their_own_ranges(self):
        """★ 核心 ★ 两套网格都下,且区间各不相同(各自的行列号)。"""
        stopped, made = self._run([GEO_GEODETIC, GEO_MERCATOR])
        self.assertFalse(stopped)
        self.assertEqual(sorted(made), [GEO_GEODETIC, GEO_MERCATOR])
        gd, mc = made[GEO_GEODETIC].ranges, made[GEO_MERCATOR].ranges
        self.assertEqual(len(gd), 2)
        self.assertEqual(len(mc), 2)
        self.assertNotEqual(gd[0][1], mc[0][1],
                            "两套网格的 row 起点相同 → 用错网格了")

    def test_annotation_follows_each_grid(self):
        """注记必须与它那套底图同网格 —— 网格选错会请求到别处的注记(不报错)。"""
        import asyncio
        made_dl, made_anno = {}, {}

        def dl_for(grid):
            return made_dl.setdefault(grid, _FakeDownloader(grid))

        def anno_for(grid):
            return made_anno.setdefault(grid, _FakeDownloader("anno-" + grid))

        asyncio.run(_download_all_grids(
            provider_key="tianditu_img", grids=[GEO_GEODETIC, GEO_MERCATOR],
            levels=[5], bbox=_BBOX, anno_levels=[5],
            downloader_for=dl_for, anno_for=anno_for,
            on_progress=lambda *a: None, should_stop=lambda: False))
        self.assertEqual(sorted(made_anno), [GEO_GEODETIC, GEO_MERCATOR])
        for grid in (GEO_GEODETIC, GEO_MERCATOR):
            self.assertEqual(made_anno[grid].ranges, made_dl[grid].ranges,
                             f"{grid} 网格的注记区间应与底图一致")

    def test_annotation_skipped_outside_anno_levels(self):
        """注记只下 ≤ z18 的级别(底图不受此限)。

        注记下载器是**预先构造**的(与改动前的结构一致),所以这里断言的是
        "没被调用",而不是"没被构造"。
        """
        import asyncio
        anno = _FakeDownloader("anno")
        asyncio.run(_download_all_grids(
            provider_key="tianditu_img", grids=[GEO_GEODETIC], levels=[19],
            bbox=_BBOX, anno_levels=[5],
            downloader_for=lambda g: _FakeDownloader(g),
            anno_for=lambda g: anno,
            on_progress=lambda *a: None, should_stop=lambda: False))
        self.assertEqual(anno.ranges, [],
                         "超出注记级别的层级不该下注记(会白跑一遍网络)")

    def test_stop_propagates_and_halts(self):
        import asyncio
        order = []

        def dl_for(grid):
            order.append(grid)
            return _StopAfterFirst()

        stopped = asyncio.run(_download_all_grids(
            provider_key="tianditu_img", grids=[GEO_GEODETIC, GEO_MERCATOR],
            levels=[5, 6], bbox=_BBOX, anno_levels=[],
            downloader_for=dl_for, anno_for=None,
            on_progress=lambda *a: None, should_stop=lambda: False))
        self.assertTrue(stopped, "被停止时应返回 True")
        self.assertEqual(order, [GEO_GEODETIC], "停止后不该继续下一个网格")


if __name__ == "__main__":
    unittest.main()
