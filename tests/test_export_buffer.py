"""导出必须也走 level_range —— 缓冲才会真正反映到瓦片包里。

实测踩到:下载阶段按 level_range 下了缓冲瓦片、预估也算对了,
但 export_tms/export_osm 内部仍用 range_for_bbox(bbox) 算区间,
于是**切出来的包只有原范围** —— 缓冲等于白下(缓存里躺着,包里没有)。
"""
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_bounds

from backend.core.tms import export_tms

BBOX = (113.6, 30.9, 113.9, 31.1)
LAT_LIMIT = 85.05112878


class _P:
    key = "tdt_test"
    ext = "png"
    bands = 3


def _tile_path(cache: Path):
    def path(col, row, z):
        p = cache / f"{z}/{col}_{row}.png"
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            a = np.full((3, 8, 8), 30, dtype=np.uint8)
            prof = {"driver": "GTiff", "height": 8, "width": 8, "count": 3,
                    "dtype": "uint8", "crs": "EPSG:4326",
                    "transform": from_bounds(-180, -90, 180, 90, 8, 8)}
            with MemoryFile() as m:
                with m.open(**prof) as ds:
                    ds.write(a)
                with m.open() as ds:
                    rio_copy(ds, str(p), driver="PNG")
        return p
    return path


def _count(out: Path) -> int:
    return sum(1 for _ in out.rglob("*.png"))


class TmsExportBufferTest(unittest.TestCase):
    def _run(self, rings: int) -> int:
        tmp = Path(tempfile.mkdtemp(prefix="tdt-buf-"))
        try:
            out = tmp / "out"
            export_tms(_P(), _tile_path(tmp / "cache"), BBOX, [12, 14], out,
                       on_progress=lambda *_: None, buffer_rings=rings)
            return _count(out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_buffer_rings_enlarge_tms_output(self):
        """★ 核心 ★ 缓冲圈数必须改变输出的瓦片数(它决定覆盖范围)。"""
        base = self._run(0)
        one = self._run(1)
        three = self._run(3)
        self.assertGreater(one, base, "缓冲 1 圈没有让 TMS 覆盖范围变大")
        self.assertGreater(three, one, "缓冲 3 圈没有比 1 圈更大")

    def test_zero_rings_matches_legacy(self):
        """buffer_rings=0 时与改动前一致(只有范围相交的瓦片)。"""
        from backend.core.tiling import range_for_bbox
        expected = sum(range_for_bbox(*BBOX, z).count for z in (12, 14))
        self.assertEqual(self._run(0), expected)


class OsmExportBufferTest(unittest.TestCase):
    """OSM 侧同样必须走 level_range(否则缓冲只在 TMS 生效)。"""

    def _src(self, tmp: Path) -> Path:
        """源图要**覆盖到缓冲区** —— 真实场景里由 _build_osm_source 按同一口径拼。

        源图只有 bbox 时,缓冲瓦片会因"完全在源范围外"被 export_osm 跳过,
        测不出缓冲是否生效。
        """
        p = tmp / "src.tif"
        arr = np.full((3, 256, 256), 40, dtype=np.uint8)
        prof = {"driver": "GTiff", "height": 256, "width": 256, "count": 3,
                "dtype": "uint8", "crs": "EPSG:4326",
                "transform": from_bounds(113.0, 30.5, 114.5, 31.5, 256, 256)}
        import rasterio
        with rasterio.open(p, "w", **prof) as ds:
            ds.write(arr)
        return p

    def _run(self, rings: int) -> int:
        tmp = Path(tempfile.mkdtemp(prefix="tdt-obuf-"))
        try:
            from backend.core.osm import export_osm
            out = tmp / "out"
            export_osm(self._src(tmp), [12, 14], BBOX, out,
                       on_progress=lambda *_: None, buffer_rings=rings)
            return _count(out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_buffer_rings_enlarge_osm_output(self):
        base = self._run(0)
        one = self._run(1)
        self.assertGreater(one, base, "缓冲 1 圈没有让 OSM 覆盖范围变大")


if __name__ == "__main__":
    unittest.main()
