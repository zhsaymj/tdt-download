"""EPSG:3857 墨卡托 XYZ 网格数学。

重点是两条回归护栏:
  1. 与 dem_tiling / osm 原有实现逐点一致(本模块是从它们抽出来的)
  2. dem_tiling 的旧名字仍可导入(避免破坏现有调用)
"""
import math
import unittest

from backend.core.mercator_tiling import (
    LAT_LIMIT, MERC_MAX, TILE_SIZE, estimate_mercator_tiles, lonlat_to_xyz,
    mercator_range_for_bbox, mosaic_bounds_3857, tile_bounds_3857,
)


class TestLonlatToXyz(unittest.TestCase):
    def test_origin_top_left(self):
        """z=1 时左上象限是 (0, 0)。"""
        self.assertEqual(lonlat_to_xyz(-179.0, 80.0, 1), (0, 0))

    def test_z0_single_tile(self):
        self.assertEqual(lonlat_to_xyz(0.0, 0.0, 0), (0, 0))

    def test_beijing_z16(self):
        """北京 (116.386, 40.0) 在 z16 的瓦片号(实测基准值)。"""
        self.assertEqual(lonlat_to_xyz(116.386, 40.0, 16), (53955, 24810))

    def test_lat_clamped_to_limit(self):
        """超出墨卡托纬度上限时夹紧,不应抛异常或产生越界瓦片号。"""
        x, y = lonlat_to_xyz(0.0, 89.9, 5)
        self.assertGreaterEqual(y, 0)
        self.assertEqual(y, lonlat_to_xyz(0.0, LAT_LIMIT, 5)[1])

    def test_clamped_within_matrix(self):
        """经度 180 边界不应越界到 2^z。"""
        n = 2 ** 4
        x, y = lonlat_to_xyz(180.0, 0.0, 4)
        self.assertLess(x, n)


class TestTileBounds(unittest.TestCase):
    def test_z0_covers_world(self):
        minx, miny, maxx, maxy = tile_bounds_3857(0, 0, 0)
        self.assertAlmostEqual(minx, -MERC_MAX, places=6)
        self.assertAlmostEqual(maxx, MERC_MAX, places=6)
        self.assertAlmostEqual(maxy, MERC_MAX, places=6)
        self.assertAlmostEqual(miny, -MERC_MAX, places=6)

    def test_roundtrip_lonlat_to_tile_to_bounds(self):
        """经纬度 -> 瓦片 -> 四至:原点应落在该瓦片范围内。"""
        lon, lat, z = 116.386, 40.0, 14
        x, y = lonlat_to_xyz(lon, lat, z)
        minx, miny, maxx, maxy = tile_bounds_3857(x, y, z)
        mx = MERC_MAX * lon / 180.0
        my = (MERC_MAX * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))
              / math.pi)
        self.assertTrue(minx <= mx <= maxx)
        self.assertTrue(miny <= my <= maxy)


class TestRangeForBbox(unittest.TestCase):
    def test_single_tile_range(self):
        tr = mercator_range_for_bbox(116.386, 40.000, 116.3861, 40.0001, 10)
        self.assertEqual(tr.count, 1)
        self.assertEqual(tr.z, 10)

    def test_known_counts(self):
        """0.05 度选区的逐级瓦片数(设计 §3.8 实测基准)。"""
        bbox = (116.36, 39.98, 116.41, 40.03)
        self.assertEqual(mercator_range_for_bbox(*bbox, 18).count, 1862)
        self.assertEqual(mercator_range_for_bbox(*bbox, 19).count, 7104)

    def test_mosaic_bounds_matches_corner_tiles(self):
        tr = mercator_range_for_bbox(116.36, 39.98, 116.41, 40.03, 12)
        minx, miny, maxx, maxy = mosaic_bounds_3857(tr)
        tl = tile_bounds_3857(tr.col_min, tr.row_min, tr.z)
        br = tile_bounds_3857(tr.col_max, tr.row_max, tr.z)
        self.assertAlmostEqual(minx, tl[0], places=6)
        self.assertAlmostEqual(maxy, tl[3], places=6)
        self.assertAlmostEqual(maxx, br[2], places=6)
        self.assertAlmostEqual(miny, br[1], places=6)


class TestEstimate(unittest.TestCase):
    def test_sums_per_level(self):
        bbox = (116.36, 39.98, 116.41, 40.03)
        got = estimate_mercator_tiles(bbox, [18, 19])
        self.assertEqual(got, 1862 + 7104)

    def test_empty_levels(self):
        self.assertEqual(estimate_mercator_tiles((0, 0, 1, 1), []), 0)


class TestBackwardCompat(unittest.TestCase):
    """回归护栏:抽出后旧调用路径必须不变。"""

    def test_dem_tiling_reexports(self):
        from backend.core import dem_tiling
        self.assertIs(dem_tiling.lonlat_to_xyz, lonlat_to_xyz)
        self.assertIs(dem_tiling.mercator_range_for_bbox, mercator_range_for_bbox)
        self.assertIs(dem_tiling.mosaic_bounds_3857, mosaic_bounds_3857)
        self.assertIs(dem_tiling.tile_bounds_3857, tile_bounds_3857)

    def test_dem_tiling_old_estimate_name_kept(self):
        """estimate_dem_tiles 是 api/tasks.py 正在用的名字,不能消失。"""
        from backend.core.dem_tiling import estimate_dem_tiles
        bbox = (116.36, 39.98, 116.41, 40.03)
        self.assertEqual(estimate_dem_tiles(bbox, [18]),
                         estimate_mercator_tiles(bbox, [18]))

    def test_osm_uses_shared_impl(self):
        """osm.py 不应再有自己的一份实现。"""
        from backend.core import osm
        self.assertIs(osm.lonlat_to_xyz, lonlat_to_xyz)
        self.assertIs(osm.tile_bounds_3857, tile_bounds_3857)

    def test_constants_agree(self):
        self.assertEqual(TILE_SIZE, 256)
        self.assertAlmostEqual(MERC_MAX, 20037508.342789244, places=6)
        self.assertAlmostEqual(LAT_LIMIT, 85.05112878, places=8)


if __name__ == "__main__":
    unittest.main()
