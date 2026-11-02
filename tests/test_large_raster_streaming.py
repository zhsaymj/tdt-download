"""大栅格必须分块流式处理(防回归)。

背景:全国 z13 地形任务在"提取等高线"和"切 Cesium 地形"两个阶段都失败,报
CPLE_OutOfMemoryError: cannot allocate 262144 bytes。256KB 的分配失败只是表症,
真因是两处都把整幅栅格读进内存:

  * postprocess.reproject_geotiff:src.read(i) 整幅 46336x29440 float32 = 5.08GB,
    目标 4326 再要 4.55GB;
  * contour.extract_contours:masked 读 5.08GB 后再 astype("float64") 要 10.9GB。

两者现已改为分块 / WarpedVRT 流式,峰值与栅格尺寸无关。本文件锁定这两条性质:
块划分无重无漏、分块结果与整幅一致、等高线分带结果与整幅等价。

不用 output/ 里的真实成果做输入 —— 那些随时会被清理,换台机器就失败。
"""
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin
from rasterio.warp import calculate_default_transform, reproject

import backend.core.contour as contour_mod
import backend.core.postprocess as pp


def _write_fixture(path: Path, size: int = 220, nodata: float = -32768.0) -> None:
    """起伏地形 + 两块 nodata 空洞(覆盖"掩膜必须一起搬"的分支)。"""
    yy, xx = np.mgrid[0:size, 0:size]
    elev = (np.sin(xx / 21.0) * 70 + np.cos(yy / 27.0) * 110 + yy * 0.4)
    elev = elev.astype("float32")
    elev[:30, :38] = nodata
    elev[size - 40:, size - 50:] = nodata
    with rasterio.open(
        path, "w", driver="GTiff", height=size, width=size, count=1,
        dtype="float32", crs="EPSG:4326",
        transform=from_origin(114.0, 31.0, 0.001, 0.001), nodata=nodata,
    ) as dst:
        dst.write(elev, 1)


def _reference_extract(path: Path, interval: float):
    """整幅实现(features.shapes 直读全图),作为等价性基准。"""
    from rasterio import features
    from shapely.geometry import shape

    with rasterio.open(path) as src:
        elev = src.read(1, masked=True)
        transform = src.transform
        px = abs(transform.a)
    vmin, vmax = float(elev.min()), float(elev.max())
    arr = np.ma.filled(elev, vmin - 1.0).astype("float64")
    levels = np.arange(np.ceil(vmin / interval) * interval, vmax, interval)
    out = []
    for lv in levels:
        mask = (arr >= lv)
        if not mask.any() or mask.all():
            continue
        m8 = mask.astype("uint8")
        for geom, _v in features.shapes(m8, mask=mask, transform=transform):
            boundary = shape(geom).boundary
            if boundary.is_empty:
                continue
            parts = ([boundary] if boundary.geom_type == "LineString"
                     else list(boundary.geoms))
            for part in parts:
                part = part.simplify(px, preserve_topology=False)
                if not part.is_empty and len(part.coords) >= 2:
                    out.append(part)
    return out


class ChunkWindowsTest(unittest.TestCase):
    """块划分必须完整覆盖且互不重叠 —— 否则会漏写像素或重复写。"""

    def test_covers_raster_exactly_once(self):
        width, height, chunk = 1000, 700, 256
        seen = np.zeros((height, width), dtype=np.int32)
        for win in pp._chunk_windows(width, height, chunk):
            seen[win.row_off:win.row_off + win.height,
                 win.col_off:win.col_off + win.width] += 1
        self.assertEqual(seen.min(), 1)
        self.assertEqual(seen.max(), 1)

    def test_handles_exact_multiple(self):
        wins = list(pp._chunk_windows(512, 512, 256))
        self.assertEqual(len(wins), 4)
        for win in wins:
            self.assertEqual((win.width, win.height), (256, 256))


class ReprojectStreamingTest(unittest.TestCase):
    """分块重投影的结果要与整幅重投影一致(块接缝不能出错)。"""

    def test_chunked_matches_whole_raster(self):
        with TemporaryDirectory() as td:
            src_path = Path(td) / "src.tif"
            _write_fixture(src_path, size=200)

            # 强制切很多小块,放大接缝问题的暴露面
            with unittest.mock.patch.object(pp, "_CHUNK_PX", 48):
                self.assertTrue(pp.reproject_geotiff(src_path, "EPSG:3857"))

            ref_path = Path(td) / "ref.tif"
            with rasterio.open(src_path) as src:
                transform, width, height = calculate_default_transform(
                    src.crs, "EPSG:3857", src.width, src.height, *src.bounds)
                profile = src.profile.copy()
                profile.update(crs="EPSG:3857", transform=transform,
                               width=width, height=height)
                bands = [src.read(i) for i in range(1, src.count + 1)]
                src_tf, src_crs, nodata = src.transform, src.crs, src.nodata
            with rasterio.open(ref_path, "w", **profile) as dst:
                for i, band in enumerate(bands, start=1):
                    reproject(
                        source=band, destination=rasterio.band(dst, i),
                        src_transform=src_tf, src_crs=src_crs,
                        dst_transform=transform, dst_crs="EPSG:3857",
                        src_nodata=nodata, dst_nodata=nodata,
                        resampling=Resampling.bilinear,
                    )

            with rasterio.open(src_path) as a, rasterio.open(ref_path) as b:
                self.assertEqual(a.crs.to_string(), "EPSG:3857")
                self.assertEqual((a.width, a.height), (b.width, b.height))
                da, db = a.read(1), b.read(1)
            diff = int((da != db).sum())
            self.assertLess(diff / da.size, 0.01,
                            "分块重投影与整幅结果差异过大:%d/%d" % (diff, da.size))


class ContourBandStreamingTest(unittest.TestCase):
    """分带等高线要与整幅实现几何等价 —— 带边界不能留假线,也不能错位。"""

    def test_band_split_equals_whole_raster(self):
        from shapely import wkb
        from shapely.ops import unary_union

        with TemporaryDirectory() as td:
            src_path = Path(td) / "dem.tif"
            _write_fixture(src_path, size=260)
            interval = 20.0

            ref = _reference_extract(src_path, interval)

            # 强制小带高 + 小重叠,确保跨十几条带
            with (
                unittest.mock.patch.object(contour_mod, "_BAND_ROWS", 32),
                unittest.mock.patch.object(contour_mod, "_BAND_OVERLAP", 8),
            ):
                geoms, values, crs = contour_mod.extract_contours(
                    src_path, interval=interval)

            new = [wkb.loads(g) for g in geoms]
            self.assertTrue(new, "分带提取没有产出任何等高线")
            self.assertEqual(len(new), len(values))
            self.assertEqual(crs, "EPSG:4326")

            ref_u = unary_union(ref)
            new_u = unary_union(new)
            tol = 0.004          # 4 个像素
            extra = new_u.difference(ref_u.buffer(tol)).length
            missing = ref_u.difference(new_u.buffer(tol)).length
            total = ref_u.length
            self.assertLess(extra, 0.01 * total,
                            "分带结果多出 %.5f 长度(带边界假线/错位)" % extra)
            self.assertLess(missing, 0.01 * total,
                            "分带结果漏掉 %.5f 长度" % missing)


if __name__ == "__main__":
    unittest.main()
