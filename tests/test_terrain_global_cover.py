"""地形切片必须是全球完整的(防回归)。

背景:全国任务切完地形后,Cesium 预览里地球是残缺的 —— 只有 bbox 范围内有
地形,其他地方一片空白。根因是 export_terrain / write_layer_json 都只按 bbox
枚举和声明瓦片:实测该任务 L0 只出了 1 张(available 里 startX=endX=1),而
geodetic 网格 L0 本该有 2 张铺满全球。Cesium 只渲染"确实有瓦片"的区域。

现在的规则:L <= GLOBAL_COVER_LEVEL 的层级按全球出瓦片(源范围外由零高程
平面填充,单个 164B),更高层级仍只出 bbox 内 —— 远处粗、近处精。

本文件锁定:低层级铺满全球、layer.json 声明与之同步、兜底可关闭。
"""
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.transform import from_origin

from backend.core.terrain_tiles import (
    GLOBAL_COVER_LEVEL,
    _encode_terrain,
    _tile_vertex_count,
    coverage_bbox_for_level,
    export_terrain,
    geodetic_range_for_bbox,
    geodetic_tile_bounds,
    write_layer_json,
)

#: 一小块中国范围的 DEM(100-110E, 25-35N)
BBOX = (100.0, 25.0, 110.0, 35.0)


def _write_dem(path: Path, size: int = 200) -> None:
    yy, xx = np.mgrid[0:size, 0:size]
    elev = (np.sin(xx / 25.0) * 300 + np.cos(yy / 30.0) * 500 + 800).astype("float32")
    elev[:30, :30] = -32768.0
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_origin(100.0, 35.0, 10.0 / size, 10.0 / size),
        nodata=-32768.0,
    ) as dst:
        dst.write(elev, 1)


class CoverageBboxTest(unittest.TestCase):
    """层级 -> 输出范围的规则。"""

    def test_low_levels_cover_globe(self):
        for L in range(0, GLOBAL_COVER_LEVEL + 1):
            self.assertEqual(coverage_bbox_for_level(BBOX, L),
                             (-180.0, -90.0, 180.0, 90.0))

    def test_high_levels_stay_in_bbox(self):
        self.assertEqual(coverage_bbox_for_level(BBOX, GLOBAL_COVER_LEVEL + 1), BBOX)
        self.assertEqual(coverage_bbox_for_level(BBOX, GLOBAL_COVER_LEVEL + 40), BBOX)

    def test_negative_level_disables_fallback(self):
        for L in range(0, 5):
            self.assertEqual(coverage_bbox_for_level(BBOX, L, -1), BBOX)

    def test_global_bbox_yields_full_grid(self):
        """全球 bbox 在每级都应恰好是满覆盖,不多一格也不少一格。"""
        for L in range(0, 6):
            x_min, x_max, y_min, y_max = geodetic_range_for_bbox(
                (-180.0, -90.0, 180.0, 90.0), L)
            self.assertEqual((x_min, x_max, y_min, y_max),
                             (0, 2 ** (L + 1) - 1, 0, 2 ** L - 1))


class GlobalExportTest(unittest.TestCase):
    """切片产物与 layer.json 声明必须一致,否则 Cesium 不会去请求兜底瓦片。"""

    def test_export_fills_low_levels_globally(self):
        with TemporaryDirectory() as td:
            dem = Path(td) / "dem.tif"
            _write_dem(dem)
            out = Path(td) / "terrain"
            _, exported, stopped = export_terrain(
                dem, [0, 1, 2], BBOX, out, global_cover_level=2)
            self.assertFalse(stopped)
            for L, want in {0: 2, 1: 8, 2: 32}.items():
                got = len(list((out / str(L)).rglob("*.terrain")))
                self.assertEqual(got, want, "L%d 应铺满全球" % L)

            write_layer_json(out, BBOX, exported, global_cover_level=2)
            layer = json.loads((out / "layer.json").read_text(encoding="utf-8"))
            self.assertEqual(layer["available"][0],
                             [{"startX": 0, "startY": 0, "endX": 1, "endY": 0}])
            self.assertEqual(layer["available"][2],
                             [{"startX": 0, "startY": 0, "endX": 7, "endY": 3}])

            # 西半球那张必须真实存在,而且必须是**密集网格**。
            # 这条断言是本文件的核心:早先零高程瓦片走 Martini,被简化成 4 个
            # 顶点、2 个三角形(164B),跨 180° 的 L0 瓦片只剩 4 个角点连成的
            # 平面弦,整个地球渲染出来是多面体。129x129 规则网格约 194KB。
            far = out / "0" / "0" / "0.terrain"
            self.assertTrue(far.exists(), "地球背面缺瓦片")
            n = 129   # L0 跨度 180°,按 1.5°/格 -> 129 边长
            # 88B 头 + 8B 计数 + 顶点(u,v,h 各 uint16)+ 索引(每三角形 3 个 uint16)
            expect = 96 + n * n * 6 + 2 * (n - 1) ** 2 * 3 * 2
            self.assertGreater(
                far.stat().st_size, expect * 0.8,
                "L0 兜底瓦片只有 %d 字节,说明被简化成了平面(地球会是多面体)"
                % far.stat().st_size)

    def test_declared_range_matches_files_on_disk(self):
        """layer.json 声明的每一张瓦片都要真的存在 —— 少一张 Cesium 就 404。"""
        with TemporaryDirectory() as td:
            dem = Path(td) / "dem.tif"
            _write_dem(dem)
            out = Path(td) / "terrain"
            _, exported, _ = export_terrain(
                dem, [0, 1], BBOX, out, global_cover_level=1)
            write_layer_json(out, BBOX, exported, global_cover_level=1)
            layer = json.loads((out / "layer.json").read_text(encoding="utf-8"))
            for L, ranges in enumerate(layer["available"]):
                for r in ranges:
                    for x in range(r["startX"], r["endX"] + 1):
                        for y in range(r["startY"], r["endY"] + 1):
                            tile = out / str(L) / str(x) / ("%d.terrain" % y)
                            self.assertTrue(tile.exists(),
                                            "声明了但缺失:%s" % tile)

    def test_stale_flat_tiles_are_resliced(self):
        """旧的 4 顶点平面瓦片必须被重切 —— 否则用户重跑也修不好多面体。"""
        with TemporaryDirectory() as td:
            dem = Path(td) / "dem.tif"
            _write_dem(dem)
            out = Path(td) / "terrain"

            # 手工放一张旧式空瓦片:旧实现让零高程网格走 Martini,被简化成
            # 4 顶点平面 —— 这正是"地球是多面体"的元凶。
            stale = out / "0" / "0" / "0.terrain"
            stale.parent.mkdir(parents=True, exist_ok=True)
            stale.write_bytes(_encode_terrain(
                np.zeros((3, 3), dtype=np.float32),
                geodetic_tile_bounds(0, 0, 0), 5.0))
            self.assertLess(_tile_vertex_count(stale), 100)

            # 再按新行为切同一目录:断点续切不能把它当"已完成"跳过
            export_terrain(dem, [0], BBOX, out, global_cover_level=0)
            self.assertGreaterEqual(
                _tile_vertex_count(stale), 129 * 129,
                "旧平面瓦片没有被重切,地球仍会是多面体")

    def test_fresh_flat_tiles_are_reused(self):
        """已达标的瓦片要正常跳过,别把断点续切搞失效。"""
        with TemporaryDirectory() as td:
            dem = Path(td) / "dem.tif"
            _write_dem(dem)
            out = Path(td) / "terrain"
            export_terrain(dem, [0], BBOX, out, global_cover_level=0)
            tile = out / "0" / "0" / "0.terrain"
            stamp = tile.stat().st_mtime_ns
            time.sleep(0.02)
            export_terrain(dem, [0], BBOX, out, global_cover_level=0)
            self.assertEqual(tile.stat().st_mtime_ns, stamp)

    def test_can_disable_global_cover(self):
        """关掉兜底要退化成旧行为(只出 bbox 内)。"""
        with TemporaryDirectory() as td:
            dem = Path(td) / "dem.tif"
            _write_dem(dem)
            out = Path(td) / "terrain"
            _, exported, _ = export_terrain(
                dem, [0], BBOX, out, global_cover_level=-1)
            self.assertEqual(
                len(list((out / "0").rglob("*.terrain"))), 1)
            write_layer_json(out, BBOX, exported, global_cover_level=-1)
            layer = json.loads((out / "layer.json").read_text(encoding="utf-8"))
            self.assertEqual(layer["available"][0],
                             [{"startX": 1, "startY": 0, "endX": 1, "endY": 0}])


if __name__ == "__main__":
    unittest.main()
