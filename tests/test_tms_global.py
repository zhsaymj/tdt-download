"""TMS 全球段与局部段在同一目录树,级号映射一致(设计 3.4 / D5)。"""
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
from rasterio.io import MemoryFile
from rasterio.shutil import copy as rio_copy
from rasterio.transform import from_bounds

from backend.core.tms import export_tms, tms_level, tms_row


class TmsGlobalTest(unittest.TestCase):
    def _fake_provider(self):
        """一个最简单的 tianditu_img 风格 provider(图片格式是 png)。"""
        class P:
            key = "tdt_test"
            ext = "png"
            bands = 3
        return P()

    def _fake_tile_path(self, cache: Path):
        """生成假缓存瓦片(8×8 单色 PNG)。"""
        def path(col, row, z):
            p = cache / f"{z}/{col}_{row}.png"
            if p.exists() and p.stat().st_size > 0:
                return p
            p.parent.mkdir(parents=True, exist_ok=True)
            arr = np.full((3, 8, 8), 30, dtype=np.uint8)
            prof = {"driver": "GTiff", "height": 8, "width": 8,
                    "count": 3, "dtype": "uint8", "crs": "EPSG:4326",
                    "transform": from_bounds(-180, -90, 180, 90, 8, 8)}
            with MemoryFile() as m:
                with m.open(**prof) as ds:
                    ds.write(arr)
                with m.open() as ds:
                    rio_copy(ds, str(p), driver="PNG")
            return p
        return path

    def test_two_segments_merge_in_one_tree(self):
        """全球段(z1..5,全球 bbox)与局部段(z10,局部 bbox)写同一目录后文件并存。"""
        tmp = Path(tempfile.mkdtemp(prefix="tdt-tmsg-"))
        cache = tmp / "cache"
        out = tmp / "out"
        tile_path = self._fake_tile_path(cache)
        P = self._fake_provider()

        # 全球段 z1-5(实际只切几张代表验证级号映射)
        _, gz, _, gstop = export_tms(
            P, tile_path, (-180, -90, 180, 90), [5], out,
            on_progress=lambda *_: None)
        self.assertEqual(gz, [4])           # z5 → TMS 级号 4

        # 局部段 z12
        _, lz, _, lstop = export_tms(
            P, tile_path, (120.5, 30.5, 121.0, 30.9), [12], out,
            on_progress=lambda *_: None)
        self.assertEqual(lz, [11])           # z12 → TMS 级号 11

        # 全球段 z5 → TMS 级 4,行号翻转
        tlv = tms_level(5)
        self.assertEqual(tlv, 4)
        self.assertTrue((out / str(tlv)).exists(), f"全球段目录{out/str(tlv)}不存在")
        # z12 → TMS 级 11
        self.assertEqual(tms_level(12), 11)
        self.assertTrue((out / str(11)).exists(), f"局部段目录{out/str(11)}不存在")
        # 清理
        shutil.rmtree(tmp, ignore_errors=True)

    def test_global_low_level_math(self):
        """tms_level / tms_row 换算一致:z5 行 0 → ty 15。"""
        self.assertEqual(tms_level(5), 4)
        self.assertEqual(tms_row(0, 5), 15)
        self.assertEqual(tms_row(15, 5), 0)


if __name__ == "__main__":
    unittest.main()