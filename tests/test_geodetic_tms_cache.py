"""Google/Esri 低层级 TMS 由重投影缓存补齐,之后复用(设计 D5 / 3.4)。"""
import shutil, tempfile, unittest
from pathlib import Path

import numpy as np
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_bounds

from backend.core.geodetic_tms_cache import ensure_geodetic_tms_cache


class GeodeticTmsCacheTest(unittest.TestCase):
    def _mercator_cache(self, root: Path, z: int):
        """造 z1 3857 缓存(4 张 4×4 单色 PNG)。"""
        def path(x, y, zz):
            p = root / f"{zz}/{x}_{y}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                a = np.full((3, 4, 4), 30, dtype=np.uint8)
                prof = {"driver": "GTiff", "height": 4, "width": 4,
                        "count": 3, "dtype": "uint8", "crs": "EPSG:3857",
                        "transform": from_bounds(-180, -85.05, 180, 85.05, 4, 4)}
                with MemoryFile() as m:
                    with m.open(**prof) as ds:
                        ds.write(a)
                    with m.open() as ds:
                        rio_copy(ds, str(p), driver="PNG")
            return p
        return path

    def test_creates_and_reuses(self):
        tmp = Path(tempfile.mkdtemp(prefix="tdt-gtc-"))
        cache = tmp / "cache"
        tile_path = self._mercator_cache(cache, 1)
        class P:
            key = "google_img"; ext = "png"; bands = 3
        d1 = ensure_geodetic_tms_cache(
            cache, "google_img", 1, (-180, -85.05, 180, 85.05),
            tile_path, P(), levels_for_src=[1])
        self.assertTrue((d1 / "_ready.txt").exists())
        # 第二次应复用
        d2 = ensure_geodetic_tms_cache(
            cache, "google_img", 1, (-180, -85.05, 180, 85.05),
            tile_path, P(), levels_for_src=[1])
        self.assertEqual(d1, d2)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()