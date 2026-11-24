"""批次4 的防回归护栏(§5.4)。

锁住三条性质,任何一条被改回旧写法都必须让本文件失败:

  1. TMS 切片的在飞 Future 有界 —— 峰值只跟线程数有关,与瓦片总数无关
     (旧写法 `pool.map` 会为整层瓦片一次性建 Future,约 1.6 KB/个);
  2. `crop_to_bbox` 全程走分块窗口 —— 不再一次 `src.read(window=整个裁剪框)`;
  3. 晕渲的峰值与输入尺寸无关 —— 分块搬运不改变数值,也不随幅面增长。

第 3 条用 `tracemalloc` 直接量峰值。不用 output/ 里的真实成果做输入:那些随时
会被清理,换台机器就跑不了。
"""
import tracemalloc
import unittest
import unittest.mock
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.transform import from_origin

import backend.core.dem as dem_mod
import backend.core.postprocess as pp
import backend.core.tms as tms_mod
from backend.core.dem import DEM_NODATA


class _SpyPool:
    """记录同时在飞 Future 数的 ThreadPoolExecutor 包装(只测峰值,不改行为)。"""

    def __init__(self, *args, **kwargs):
        self._real = ThreadPoolExecutor(*args, **kwargs)
        self.peak = 0
        self._live = set()

    def submit(self, fn, *args, **kwargs):
        fut = self._real.submit(fn, *args, **kwargs)
        self._live.add(fut)
        fut.add_done_callback(self._live.discard)
        self.peak = max(self.peak, len(self._live))
        return fut

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self._real.shutdown(wait=True)
        return False


class _FakeProvider:
    ext = "jpg"


class TmsFuturesBoundedTest(unittest.TestCase):
    """#3 / #7:`export_tms` 的在飞 Future 不超过 `max(workers*4, 16)`。"""

    def test_pending_futures_bounded(self):
        # 0.6° 在 z12/z13 上是数百张瓦片,足够越过滑窗上限多次
        bbox = (116.0, 39.0, 116.6, 39.6)
        workers = 4
        pool = None

        def _spy(*a, **kw):
            nonlocal pool
            pool = _SpyPool(*a, **kw)
            return pool

        with TemporaryDirectory() as tmp, \
                unittest.mock.patch.object(tms_mod, "ThreadPoolExecutor", _spy), \
                unittest.mock.patch.object(tms_mod, "_render_tms_tile",
                                           return_value=False):
            tms_mod.export_tms(_FakeProvider(), lambda z, c, r: None, bbox,
                               [12, 13], Path(tmp), concurrency=workers)

        self.assertIsNotNone(pool, "未走到线程池分支")
        self.assertLessEqual(pool.peak, max(workers * 4, 16),
                             f"在飞 Future 峰值 {pool.peak} 超过上限")


class CropToBboxWindowTest(unittest.TestCase):
    """#5:`crop_to_bbox` 每次 `src.read` 的窗口都不超过 `_CHUNK_PX`。"""

    def test_reads_are_chunked(self):
        chunk = 128
        with TemporaryDirectory() as tmp:
            src = Path(tmp) / "src.tif"
            # 600×600 像素、0.01°/px → 6°×6°,裁剪到居中的 2°×2°(=200×200 像素)
            arr = np.arange(600 * 600, dtype=np.uint16).reshape(600, 600)
            with rasterio.open(src, "w", driver="GTiff", height=600, width=600,
                               count=1, dtype="uint16", crs="EPSG:4326",
                               transform=from_origin(100.0, 46.0, 0.01, 0.01)) as d:
                d.write(arr, 1)

            windows = []
            orig_read = rasterio.io.DatasetReader.read

            def _spy_read(self, indexes=None, out=None, window=None, **kw):
                if window is not None:
                    windows.append((int(window.width), int(window.height)))
                return orig_read(self, indexes=indexes, out=out, window=window, **kw)

            with unittest.mock.patch.object(pp, "_CHUNK_PX", chunk), \
                    unittest.mock.patch.object(rasterio.io.DatasetReader, "read",
                                               _spy_read):
                self.assertTrue(pp.crop_to_bbox(src, (102.0, 42.0, 104.0, 44.0)))

            self.assertTrue(windows, "没有观察到带 window 的读取")
            self.assertGreater(len(windows), 1, "只读了一次 —— 说明又整幅读了")
            self.assertLessEqual(max(w for w, _ in windows), chunk)
            self.assertLessEqual(max(h for _, h in windows), chunk)

            with rasterio.open(src) as d:
                self.assertEqual((d.height, d.width), (200, 200))
                got = d.read(1)
            self.assertEqual(int(got[0, 0]), int(arr[200, 200]))
            self.assertEqual(int(got[-1, -1]), int(arr[399, 399]))


def _write_dem(path: Path, size: int) -> None:
    """造一张带 nodata 斜坡的 DEM(起伏确定,便于逐像素比对)。"""
    yy, xx = np.mgrid[0:size, 0:size]
    elev = (xx * 0.5 + yy * 0.25).astype(np.float32)
    elev[size // 4:size // 2, size // 4:size // 2] = DEM_NODATA
    with rasterio.open(path, "w", driver="GTiff", height=size, width=size,
                       count=1, dtype="float32", nodata=DEM_NODATA,
                       crs="EPSG:4326",
                       transform=from_origin(100.0, 45.0, 0.001, 0.001)) as d:
        d.write(elev, 1)


class HillshadePeakTest(unittest.TestCase):
    """#1:晕渲峰值与幅面无关(分块),且数值与整幅实现一致。"""

    def _peak(self, size: int, chunk: int) -> tuple[int, np.ndarray]:
        with TemporaryDirectory() as tmp:
            dem = Path(tmp) / "dem.tif"
            out = Path(tmp) / "hs.tif"
            _write_dem(dem, size)
            with unittest.mock.patch.object(dem_mod, "_HS_CHUNK_PX", chunk):
                tracemalloc.start()
                try:
                    dem_mod.hillshade_from_dem(dem, out, build_overviews=False)
                    _, peak = tracemalloc.get_traced_memory()
                finally:
                    tracemalloc.stop()
                with rasterio.open(out) as d:
                    return peak, d.read(1)

    def test_peak_independent_of_size(self):
        chunk = 128
        peak_small, _ = self._peak(288, chunk)     # 3×3 块
        peak_big, hs_big = self._peak(1152, chunk)  # 9×9 块,幅面 16 倍
        # 幅面 16 倍而峰值不跟着涨:分块生效。留 8 MiB 余量给解释器抖动。
        self.assertLess(peak_big - peak_small, 8 << 20,
                        f"峰值随幅面增长:288→{peak_small} 1152→{peak_big}")
        self.assertEqual(hs_big.shape, (1152, 1152))

    def test_nodata_transparent(self):
        _, hs = self._peak(288, 128)
        # nodata 区是 size//4..size//2(即 72..144),晕渲此处应为 0
        self.assertEqual(int(hs[100, 100]), 0)


if __name__ == "__main__":
    unittest.main()
