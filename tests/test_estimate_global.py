"""预估与下载用同一 level_range —— 分母不会再对不上(需求39 护栏)。"""
import unittest

from backend.api.tasks import _estimate_total
from backend.core.tile_estimate import tile_total
from backend.core.tile_range import download_levels, level_range
from backend.core.tiling import range_for_bbox

BBOX = (120.5, 30.5, 121.0, 30.9)
P = "tianditu_img"


class EstimateGlobalTest(unittest.TestCase):
    def _manual(self, grids, levels, gmax, rings):
        """用 level_range 手工反算,作为对账基准(Review Focus #3)。"""
        n = 0
        for g in grids:
            for z in download_levels(levels, gmax):
                n += level_range(z, g, BBOX, global_max_level=gmax,
                                 buffer_rings=rings).count
        return n

    def test_tile_total_global_and_buffer(self):
        t = tile_total(P, ("tms",), BBOX, [18],
                       global_max_level=5, buffer_rings=2)
        self.assertEqual(t, self._manual(["geodetic"], [18], 5, 2))

    def test_tile_total_disabled_backward_compat(self):
        """global_max_level=0 时与改动前一致。"""
        old = tile_total(P, ("tms",), BBOX, [18])
        self.assertEqual(old, sum(range_for_bbox(*BBOX, z).count for z in (18,)))

    def test_estimate_total_accepts_global_params(self):
        t = _estimate_total(BBOX, [18], P, ("tms",),
                            global_max_level=5, buffer_rings=2)
        self.assertEqual(t, self._manual(["geodetic"], [18], 5, 2))

    def test_global_tiles_is_increment(self):
        """global_tiles = 开功能 − 不开功能(api_estimate 响应层)。"""
        base = tile_total(P, ("tms",), BBOX, [18])
        full = tile_total(P, ("tms",), BBOX, [18], global_max_level=5)
        self.assertGreater(full, base)


if __name__ == "__main__":
    unittest.main()