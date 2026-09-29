"""「该任务要下载哪些网格」的推导(设计 D1)。

导出格式各有原生网格(`tms` → gdal2tiles geodetic / `osm` → Web 墨卡托 XYZ),
而数据源能提供的网格不一样:

* 天地图:两套都有(`img_c`/`vec_c`/`ter_c` 与 `img_w`/`vec_w`/`ter_w`)
* Google/Esri:只有 3857
* 本地文件/DEM:没有下载环节

于是**跨网格的那一种格式必然要重投影**。需求37 已把"默认勾选的格式"调成各源
无损的那一种;本推导补的是"两个格式都选、而源两套都有"时**各下各的原生瓦片**,
使两者都无损。

**不落库**:推导是纯函数,预估接口与 worker 调同一个即可保持一致(与 `z_cap_of`
集中判定的做法一致)。
"""
import unittest

from backend.core.formats import (
    GEO_GEODETIC, GEO_MERCATOR, PROVIDER_GRIDS, download_grids_of,
)


class DownloadGridsTest(unittest.TestCase):
    def test_tianditu_tms_needs_only_geodetic(self):
        """只勾 tms → 只下 geodetic 那套(与源同构、无损直映射)。"""
        self.assertEqual(download_grids_of("tianditu_img", ["tms"]),
                         [GEO_GEODETIC])

    def test_tianditu_osm_needs_only_mercator(self):
        """只勾 osm → 只下 mercator 那套(3857→3857,不重投影)。"""
        self.assertEqual(download_grids_of("tianditu_img", ["osm"]),
                         [GEO_MERCATOR])

    def test_tianditu_both_needs_both(self):
        """★ 核心 ★ 两个都勾 → 两套都下,各自无损。"""
        self.assertEqual(download_grids_of("tianditu_img", ["tms", "osm"]),
                         [GEO_GEODETIC, GEO_MERCATOR])

    def test_all_three_tianditu_layers_have_both_grids(self):
        for key in ("tianditu_img", "tianditu_vec", "tianditu_ter"):
            self.assertEqual(download_grids_of(key, ["tms", "osm"]),
                             [GEO_GEODETIC, GEO_MERCATOR], key)

    def test_google_only_mercator_even_with_tms(self):
        """Google/Esri 只有 3857 —— 不凭空造网格;tms 仍由重投影得到。"""
        for key in ("google_img", "esri_imagery"):
            self.assertEqual(download_grids_of(key, ["tms", "osm"]),
                             [GEO_MERCATOR], key)

    def test_dem_only_its_own_grid(self):
        """esri_terrain 是 3857,且没有 geodetic 那套。"""
        self.assertEqual(download_grids_of("esri_terrain", ["tms", "osm"]),
                         [GEO_MERCATOR])

    def test_geotiff_does_not_change_grid(self):
        """不限网格的格式(geotiff/dem/terrain/contour)不改变结果。"""
        for fmt in ("geotiff", "contour"):
            self.assertEqual(download_grids_of("tianditu_img", [fmt, "tms"]),
                             [GEO_GEODETIC], fmt)

    def test_unknown_provider_defaults_only(self):
        """未登记的源只有自己的默认网格,不因为勾了 osm 就多下。"""
        self.assertEqual(download_grids_of("local_image", ["tms", "osm"]),
                         [GEO_GEODETIC])

    def test_empty_formats_yields_default_grid_only(self):
        self.assertEqual(download_grids_of("tianditu_img", []), [GEO_GEODETIC])

    def test_pure_function_is_stable_on_repeat(self):
        """同一输入两次调用结果一致(顺序稳定,下载循环才可预期)。"""
        a = download_grids_of("tianditu_img", ["osm", "tms"])
        b = download_grids_of("tianditu_img", ["tms", "osm"])
        self.assertEqual(a, b)

    def test_registry_only_lists_providers_with_two_grids(self):
        """登记表只该有真有两套网格的数据源 —— 别把 Google 之类写进去。"""
        self.assertEqual(set(PROVIDER_GRIDS),
                         {"tianditu_img", "tianditu_vec", "tianditu_ter"})
        for key, grids in PROVIDER_GRIDS.items():
            self.assertIn(GEO_MERCATOR, grids, key)
            self.assertIn(GEO_GEODETIC, grids, key)


if __name__ == "__main__":
    unittest.main()
