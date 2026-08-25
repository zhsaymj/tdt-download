import tempfile
import sys
import types
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_bounds


class _Ctx:
    def should_stop(self):
        return False


class LocalRasterEdgeMaskTest(unittest.TestCase):
    def test_resample_to_level_marks_pixels_outside_source_as_masked(self):
        if "pymartini" not in sys.modules:
            pymartini = types.ModuleType("pymartini")
            pymartini.Martini = object
            pymartini.rescale_positions = lambda *args, **kwargs: None
            sys.modules["pymartini"] = pymartini
        if "quantized_mesh_encoder" not in sys.modules:
            qme = types.ModuleType("quantized_mesh_encoder")
            qme.encode = lambda *args, **kwargs: b""
            sys.modules["quantized_mesh_encoder"] = qme
        from backend.core.runner import _resample_to_level
        from backend.core.tiling import TileRange

        tr = TileRange(z=4, col_min=8, col_max=8, row_min=4, row_max=4)
        west, south, east, north = tr.mosaic_bounds()
        dx, dy = east - west, north - south
        src_bounds = (
            west + dx * 0.25,
            south + dy * 0.25,
            east - dx * 0.25,
            north - dy * 0.25,
        )
        data = np.full((3, 64, 64), 120, dtype=np.uint8)

        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "source.tif"
            dst = Path(d) / "out.tif"
            with rasterio.open(
                src,
                "w",
                driver="GTiff",
                height=64,
                width=64,
                count=3,
                dtype="uint8",
                crs="EPSG:4326",
                transform=from_bounds(*src_bounds, 64, 64),
            ) as ds:
                ds.write(data)

            _resample_to_level(_Ctx(), src, dst, tr)

            with rasterio.open(dst) as ds:
                mask = ds.read_masks(1)
                pixels = ds.read()

        self.assertEqual(int(mask[0, 0]), 0)
        self.assertEqual(int(mask[-1, -1]), 0)
        self.assertEqual(int(mask[mask.shape[0] // 2, mask.shape[1] // 2]), 255)
        self.assertTrue(np.all(pixels[:, mask == 0] == 0))


class OsmPngTransparentPixelTest(unittest.TestCase):
    def test_write_png_bleeds_rgb_into_transparent_pixels(self):
        from backend.core.osm import _write_png

        with tempfile.TemporaryDirectory() as d:
            png = Path(d) / "tile.png"
            rgba = np.zeros((4, 256, 256), dtype=np.uint8)
            rgba[0, 96:160, 96:160] = 80
            rgba[1, 96:160, 96:160] = 120
            rgba[2, 96:160, 96:160] = 160
            rgba[3, 96:160, 96:160] = 255

            _write_png(png, rgba, from_bounds(0, 0, 1, 1, 256, 256))

            with rasterio.open(png) as ds:
                out = np.moveaxis(ds.read(), 0, -1)

        transparent_border = out[95, 96:160]
        self.assertTrue(np.all(transparent_border[:, 3] == 0))
        self.assertTrue(np.all(transparent_border[:, :3] == [80, 120, 160]))


if __name__ == "__main__":
    unittest.main()
