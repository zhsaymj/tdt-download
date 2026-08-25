import tempfile
import unittest
from pathlib import Path

import numpy as np
import rasterio
from rasterio.transform import from_origin


class RepairNodataTaskOutputTest(unittest.TestCase):
    def test_repair_task_output_restores_rgb_pixels_without_rewriting_values(self):
        from backend.api.tasks import _repair_task_output
        from backend.config import settings

        settings.output_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=settings.output_dir) as d:
            out_dir = Path(d)
            tif = out_dir / "sample_z13.tif"
            pixels = np.array([
                [[0, 36, 15], [12, 18, 18], [30, 0, 9]],
                [[0, 0, 0], [5, 5, 5], [7, 8, 0]],
            ], dtype=np.uint8)
            data = np.moveaxis(pixels, -1, 0)
            profile = {
                "driver": "GTiff",
                "height": data.shape[1],
                "width": data.shape[2],
                "count": 3,
                "dtype": "uint8",
                "crs": "EPSG:4326",
                "transform": from_origin(100, 30, 0.01, 0.01),
                "nodata": 0,
            }
            with rasterio.open(tif, "w", **profile) as ds:
                ds.write(data)

            result = _repair_task_output({"output_path": str(out_dir)})

            self.assertEqual(result["fixed"], 1)
            self.assertEqual(result["recovered_pixels"], 3)
            with rasterio.open(tif) as ds:
                self.assertIsNone(ds.nodata)
                np.testing.assert_array_equal(ds.read(), data)
                mask = ds.read_masks(1)
            expected_mask = np.array([
                [255, 255, 255],
                [0, 255, 255],
            ], dtype=np.uint8)
            np.testing.assert_array_equal(mask, expected_mask)


if __name__ == "__main__":
    unittest.main()
