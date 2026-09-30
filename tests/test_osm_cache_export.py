"""export_osm_from_cache:从 3857 缓存直映射 OSM XYZ(无损,无重投影)。"""
import shutil, tempfile, unittest
from pathlib import Path

import numpy as np
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_bounds

from backend.core.osm_cache import export_osm_from_cache


class OsmCacheExportTest(unittest.TestCase):
    def _tiles(self, root: Path):
        """生成 z1 全球 4 张 8×8 假缓存瓦片。"""
        def path(x, y, z):
            p = root / f"{z}/{x}_{y}.png"
            p.parent.mkdir(parents=True, exist_ok=True)
            if not p.exists():
                a = np.full((3, 8, 8), 30, dtype=np.uint8)
                prof = {"driver": "GTiff", "height": 8, "width": 8,
                        "count": 3, "dtype": "uint8", "crs": "EPSG:3857",
                        "transform": from_bounds(-180, -85.05, 180, 85.05, 8, 8)}
                with MemoryFile() as m:
                    with m.open(**prof) as ds:
                        ds.write(a)
                    with m.open() as ds:
                        rio_copy(ds, str(p), driver="PNG")
            return p
        return path

    def test_writes_xyz_tree_without_flip(self):
        """z1(4 张)直映射到 {z}/{x}/{y}.png,行号不翻转。"""
        tmp = Path(tempfile.mkdtemp(prefix="tdt-osmc-"))
        cache, out = tmp / "cache", tmp / "out"
        tile_path = self._tiles(cache)
        class P:
            key = "google_img"; ext = "png"; bands = 3
        out_dir, levels, stopped = export_osm_from_cache(
            P(), tile_path, (-180, -85.05, 180, 85.05), [1], out)
        self.assertEqual(levels, [1])
        self.assertFalse(stopped)
        for y in (0, 1):
            for x in (0, 1):
                self.assertTrue(
                    (out / "1" / str(x) / f"{y}.png").exists(),
                    f"缺 OSM 瓦片 {x}/{y}")
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()