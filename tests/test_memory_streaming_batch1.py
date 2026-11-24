"""批次1 内存优化的防回归护栏。

背景:`docs/内存审计报告.md` 的 #1/#5/#6/#7/#8/#10/#12/#31/#32 九处无界点。
它们共同的性质是「峰值随范围/瓦片总数线性增长」,单机 31.9 GiB 下大范围任务
直接 OOM。优化后必须锁住三条性质:

  * 结果与整幅实现逐像素等价(分块只是搬运方式,不改数值);
  * 峰值与瓦片总数无关(downloader / tms / osm_cache 的在飞任务有界);
  * 等高线边提取边写盘,不再全量攒列表。

不用 output/ 里的真实成果做输入 —— 那些随时会被清理,换台机器就失败。
"""
import asyncio
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.transform import from_origin

import backend.core.dem as dem_mod
import backend.core.mosaic as mosaic_mod
import backend.core.postprocess as pp
from backend.core.contour import ContourWriter
from backend.core.dem import DEM_NODATA
from backend.core.downloader import TileDownloader
from backend.core.tiling import TILE_SIZE, TileRange


class _FakeImgProvider:
    """只提供拼接所需的最小属性(真实 provider 还要 token/网络)。"""

    key = "fake"
    ext = "tif"
    bands = 3


def _write_tiles(cache: Path, tr: TileRange, missing: set) -> None:
    """每列一个唯一值,便于逐像素核对;缺失列不落文件。"""
    for col, row in tr.iter_tiles():
        if col in missing:
            continue
        path = cache / str(tr.z) / f"{col}_{row}.tif"
        path.parent.mkdir(parents=True, exist_ok=True)
        val = (col - tr.col_min) + 1
        arr = np.full((3, TILE_SIZE, TILE_SIZE), val, dtype=np.uint8)
        with rasterio.open(path, "w", driver="GTiff", height=TILE_SIZE,
                           width=TILE_SIZE, count=3, dtype="uint8") as dst:
            dst.write(arr)


class MosaicRowChunkTest(unittest.TestCase):
    """#31 / #32:行缓冲按列区间切分后仍逐像素等价。"""

    def test_mosaic_chunks_match_tile_values(self):
        # 23 列 > 8 列/块(_ROW_CHUNK_PX=2048 / 256),跨 3 个列块
        tr = TileRange(z=14, col_min=13500, col_max=13522,
                       row_min=6200, row_max=6201)
        # 故意在块首、块中、块尾和最后一列缺失,覆盖跨块缺块路径
        missing = {tr.col_min + 3, tr.col_min + 8, tr.col_min + 15, tr.col_max}
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            _write_tiles(cache, tr, missing)
            out = tmp / "mosaic.tif"
            mosaic_mod.mosaic_to_geotiff(
                _FakeImgProvider(), cache, tr, out,
                lambda col, row, z: cache / str(z) / f"{col}_{row}.tif")
            with rasterio.open(out) as ds:
                self.assertEqual(ds.width, tr.cols * TILE_SIZE)
                self.assertEqual(ds.height, tr.rows * TILE_SIZE)
                data = ds.read(1)
            for i, col in enumerate(range(tr.col_min, tr.col_max + 1)):
                column = data[:, i * TILE_SIZE + 128]
                expect = 0 if col in missing else (col - tr.col_min) + 1
                self.assertTrue(np.all(column == expect),
                                f"列 {col} 应全为 {expect}")

    def test_dem_chunks_match_tile_values(self):
        tr = TileRange(z=14, col_min=13500, col_max=13522,
                       row_min=6200, row_max=6201)
        missing = {tr.col_min + 3, tr.col_min + 8, tr.col_min + 15, tr.col_max}
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cache = tmp / "cache"
            vals = {}
            for col, row in tr.iter_tiles():
                if col in missing:
                    continue
                path = cache / str(tr.z) / f"{col}_{row}.lerc"
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"x")   # 内容由伪造的解码器给出
                vals[(col, row)] = float(col - tr.col_min + 100)

            def _fake_decode(path: Path):
                for (col, row), v in vals.items():
                    if path.name == f"{col}_{row}.lerc":
                        return np.full((TILE_SIZE, TILE_SIZE), v, dtype=np.float32)
                return None

            out = tmp / "dem.tif"
            with unittest.mock.patch.object(dem_mod, "decode_lerc", _fake_decode):
                dem_mod.mosaic_dem_geotiff(
                    tr, out,
                    lambda col, row, z: cache / str(z) / f"{col}_{row}.lerc")
            with rasterio.open(out) as ds:
                data = ds.read(1)
            for i, col in enumerate(range(tr.col_min, tr.col_max + 1)):
                column = data[:, i * TILE_SIZE + 128]
                expect = (DEM_NODATA if col in missing
                          else float(col - tr.col_min + 100))
                self.assertTrue(np.all(column == expect), f"列 {col}")


class HillshadeChunkTest(unittest.TestCase):
    """#1:分块晕渲与整幅实现逐像素等价(含 nodata 填充与全局边界)。"""

    def test_chunked_equals_whole_image(self):
        size, chunk = 260, 100          # 强制 3×3 块,覆盖内部块边界
        nodata = -32768.0
        yy, xx = np.mgrid[0:size, 0:size]
        elev = (300 + 120 * np.sin(xx / 21.0) * np.cos(yy / 17.0)
                ).astype(np.float32)
        elev[40:70, 60:90] = nodata
        elev[0:15, :] = nodata          # 顶边:全局边界没有 halo
        profile = dict(driver="GTiff", height=size, width=size, count=1,
                       dtype="float32", crs="EPSG:3857", nodata=nodata,
                       transform=from_origin(0, size * 30.0, 30.0, 30.0))
        with TemporaryDirectory() as td:
            dem = Path(td) / "dem.tif"
            with rasterio.open(dem, "w", **profile) as dst:
                dst.write(elev, 1)
            out = Path(td) / "hs.tif"
            with unittest.mock.patch.object(dem_mod, "_HS_CHUNK_PX", chunk):
                dem_mod.hillshade_from_dem(dem, out, build_overviews=False)
            with rasterio.open(out) as ds:
                got = ds.read(1)

        # 整幅实现(改造前的写法)作为基准,复用内存里的 elev 与原像元尺寸
        e = elev.astype(np.float64)
        mask = e == nodata
        fill = float(e[~mask].mean())
        e = np.where(mask, fill, e)
        dzdy, dzdx = np.gradient(e, 30.0, 30.0)
        slope = np.arctan(np.hypot(dzdx, dzdy))
        aspect = np.arctan2(dzdy, -dzdx)
        az = np.radians(360.0 - 315.0 + 90.0)
        alt = np.radians(45.0)
        shaded = (np.sin(alt) * np.cos(slope)
                  + np.cos(alt) * np.sin(slope) * np.cos(az - aspect))
        ref = (np.clip(shaded, 0.0, 1.0) * 255.0).astype(np.uint8)
        ref[mask] = 0

        diff = np.abs(ref.astype(np.int16) - got.astype(np.int16))
        self.assertEqual(int(diff.max()), 0, "分块晕渲必须逐像素等于整幅实现")
        self.assertTrue(np.all(got[mask] == 0), "nodata 区必须为 0")


class CropToBboxChunkTest(unittest.TestCase):
    """#5:按块搬窗裁剪与整窗一次读写结果一致。"""

    def test_chunked_crop_equals_expected_pixels(self):
        size = 200
        vals = (np.arange(size)[None, :] * 1000
                + np.arange(size)[:, None]).astype(np.uint16)
        with TemporaryDirectory() as td:
            path = Path(td) / "src.tif"
            with rasterio.open(path, "w", driver="GTiff", height=size,
                               width=size, count=1, dtype="uint16",
                               crs="EPSG:4326",
                               transform=from_origin(110.0, 40.0, 0.01, 0.01)) as dst:
                dst.write(vals, 1)
            # 取中间 120 列 × 90 行,跨多个 64 像素块
            with unittest.mock.patch.object(pp, "_CHUNK_PX", 64):
                changed = pp.crop_to_bbox(path, (110.2, 38.9, 111.4, 39.9))
            self.assertTrue(changed)
            with rasterio.open(path) as ds:
                got = ds.read(1)
        self.assertEqual(got.shape, (100, 120))
        # 期望:从源的第 10 行/第 20 列起的窗口
        expect = vals[10:110, 20:140]
        self.assertTrue(np.array_equal(got, expect))


class ContourWriterTest(unittest.TestCase):
    """#12:分批追加写的条数与属性值不丢不乱。"""

    def test_appends_all_batches(self):
        import shapely
        from pyogrio.raw import read
        from shapely.geometry import LineString

        with TemporaryDirectory() as td:
            out = Path(td) / "c.geojson"
            with unittest.mock.patch.object(ContourWriter, "_BATCH", 5):
                writer = ContourWriter(out, "geojson")
                for base in (0, 7, 14):
                    geoms = [shapely.to_wkb(LineString([(base + i, 0),
                                                        (base + i, 1)]))
                             for i in range(7)]
                    writer.add(geoms, [float(base + i) for i in range(7)],
                               "EPSG:4326")
                writer.close()
            self.assertEqual(writer.written, 21)
            meta, _fid, geoms, data = read(str(out))
            self.assertEqual(len(geoms), 21)
            self.assertEqual(sorted(float(v) for v in data[0]),
                             sorted(float(v) for v in
                                    [b + i for b in (0, 7, 14) for i in range(7)]))


class DownloaderInflightTest(unittest.TestCase):
    """#6:在飞 Task 数有界,不随瓦片总数线性增长。"""

    def test_inflight_tasks_are_bounded(self):
        tr = TileRange(z=12, col_min=0, col_max=19, row_min=0, row_max=19)
        total = tr.count

        with TemporaryDirectory() as td:
            dl = TileDownloader(_FakeImgProvider(), Path(td), concurrency=4)
            peak = {"tasks": 0, "active": 0, "concurrent": 0}

            async def _fake_one(session, sem, col, row, z):
                peak["tasks"] = max(peak["tasks"], len(asyncio.all_tasks()))
                peak["active"] += 1
                peak["concurrent"] = max(peak["concurrent"], peak["active"])
                await asyncio.sleep(0)
                peak["active"] -= 1
                return True

            with unittest.mock.patch.object(dl, "_one", _fake_one):
                ok, fail, stopped = asyncio.run(dl.download_range(tr))

        self.assertEqual((ok, fail, stopped), (total, 0, False))
        self.assertLess(peak["tasks"], total // 2,
                        "不应为每张瓦片各建一个 Task")
        self.assertLessEqual(peak["concurrent"], 32 + 4,
                             "在飞任务应受窗口限制")


if __name__ == "__main__":
    unittest.main()
