"""下载阶段按 level_range 取区间:全球段=整层,其余=范围+缓冲(设计 3.3)。"""
import asyncio
import unittest

from backend.core.runner import _download_all_grids
from backend.core.tile_range import download_levels, level_range
from backend.core.tiling import TileRange


class _FakeDL:
    """记录收到的 TileRange;`have` 里的层视为已缓存,跳过下载。"""

    def __init__(self, have: set = ()):
        self.calls: list[tuple[int, int]] = []
        self.have = set(have)

    async def download_range(self, tr, on_progress, should_stop):
        if tr.z not in self.have:
            self.calls.append((tr.z, tr.count))
            self.have.add(tr.z)
        return 0, 0, False


class RunnerGlobalDownloadTest(unittest.TestCase):
    def setUp(self):
        self.g = _FakeDL()
        self.a = _FakeDL()

    def _run(self, **kw):
        default = dict(
            provider_key="tianditu_img", grids=["geodetic"],
            levels=download_levels([12, 18], 5),
            bbox=(120.5, 30.5, 121.0, 30.9), anno_levels=[12],
            downloader_for=lambda g: self.g, anno_for=lambda g: self.a,
            on_progress=lambda *_: None, should_stop=lambda: False,)
        default.update(kw)
        return asyncio.run(_download_all_grids(**default))

    def test_global_level_requests_full_matrix(self):
        """全球段 z≤5 请求整层。"""
        self._run(global_max_level=5)
        z2 = next(c for c in self.g.calls if c[0] == 2)
        self.assertEqual(z2[1], 4 * 2)          # geodetic z2 = 4×2
        z5 = next(c for c in self.g.calls if c[0] == 5)
        self.assertEqual(z5[1], 32 * 16)        # geodetic z5 = 32×16

    def test_buffer_rings_enlarges(self):
        """z12 外扩后张数增大(与 range_for_bbox 比)。"""
        from backend.core.tiling import range_for_bbox
        base = range_for_bbox(120.5, 30.5, 121.0, 30.9, 12).count
        self._run(global_max_level=0, buffer_rings=2)
        n = next(c for c in self.g.calls if c[0] == 12)[1]
        self.assertGreater(n, base)

    def test_download_levels_used(self):
        """levels 包含全球段:1..5∪12,18。"""
        self._run(global_max_level=5)
        got = sorted({c[0] for c in self.g.calls})
        self.assertEqual(got, [1, 2, 3, 4, 5, 12, 18])

    def test_global_tiles_cached(self):
        """Review Focus #5:全球段已缓存 → 不重复下载。"""
        self.g = _FakeDL(have={2, 3, 4, 5})
        self._run(global_max_level=5)
        got = {c[0] for c in self.g.calls}
        self.assertNotIn(2, got)
        self.assertIn(12, got)


if __name__ == "__main__":
    unittest.main()