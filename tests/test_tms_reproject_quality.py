"""墨卡托源出 TMS 时,重投影要尽量保住细节(需求37)。

`runner._mercator_raster_source` 把 3857 拼接图重投影成 4326 给 TMS 切片用
(见该函数的说明:geodetic 网格与墨卡托不同构,不转就一张都切不出来)。
这一步原先用 `Resampling.bilinear` —— 实测高频能量只剩 68%(对比无损基准),
而 TMS 是唯一能出文字标注的瓦片格式,细笔画在双线性下最先糊掉,用户看到的
就是"下载切片后的文字标注比原始模糊很多"。

改用 cubic 后约 80%(lanczos 可到 90%,但会在高对比边缘产生振铃,文字上更明显)。

本文件用**受控的合成源**验证这个性质,不依赖 output/ 里的真实成果 ——
真实成果随时会被用户清理,那样的测试会在别的机器上失败。
"""
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import rasterio
from rasterio.enums import Resampling
from rasterio.transform import from_origin

from backend.core.postprocess import reproject_geotiff
from backend.core.runner import _mercator_raster_source

#: 合成源:EPSG:3857、z14 量级的像素大小,尺寸小到够快
_PIX = 9.5546          # 米/像素(z14 赤道分辨率)
_W = _H = 256
_ORIGIN = (12724000.0, 3568000.0)     # 任意一处 3857 坐标(武汉一带)


def _write_stripes(path: Path, period: int = 3) -> None:
    """写一张细竖条纹图。

    周期 3 px:远细于重投影核的支撑域,双线性(2×2)会把它抹平,
    cubic(4×4)能保住更多 —— 正是文字笔画的情形。
    """
    xs = np.arange(_W)
    row = np.where((xs // period) % 2 == 0, 235, 25).astype(np.uint8)
    arr = np.tile(row, (_H, 1))
    with rasterio.open(path, "w", driver="GTiff", height=_H, width=_W,
                       count=1, dtype="uint8", crs="EPSG:3857",
                       transform=from_origin(*_ORIGIN, _PIX, _PIX)) as dst:
        dst.write(arr, 1)


def _lapvar(path: Path) -> float:
    """拉普拉斯方差 —— 越大表示高频细节越多(越清晰)。"""
    with rasterio.open(path) as s:
        a = s.read(1).astype(np.float64)
    lap = (a[1:-1, 1:-1] * 4 - a[:-2, 1:-1] - a[2:, 1:-1]
           - a[1:-1, :-2] - a[1:-1, 2:])
    return float(lap.var())


class ReprojectKernelTest(unittest.TestCase):
    def test_cubic_preserves_more_detail_than_bilinear(self):
        """★ 核心 ★ 换核这件事本身要有效 —— 否则改了也白改。"""
        with TemporaryDirectory() as d:
            out = Path(d)
            src = out / "src.tif"
            _write_stripes(src)

            a = out / "bilinear.tif"
            shutil.copyfile(src, a)
            reproject_geotiff(a, "EPSG:4326", resampling=Resampling.bilinear)

            b = out / "cubic.tif"
            shutil.copyfile(src, b)
            reproject_geotiff(b, "EPSG:4326", resampling=Resampling.cubic)

            va, vb = _lapvar(a), _lapvar(b)
            self.assertGreater(
                vb, va * 1.05,
                f"cubic 未比 bilinear 保留更多细节(bilinear={va:.1f}, "
                f"cubic={vb:.1f}) —— 换核没起到作用")

    def test_default_resampling_stays_bilinear(self):
        """默认值不能变:reproject_geotiff 也服务用户选的输出坐标系,
        那里改核是另一件事,不该被本需求顺手改掉。"""
        import inspect
        sig = inspect.signature(reproject_geotiff)
        self.assertIs(sig.parameters["resampling"].default, Resampling.bilinear)


class MercatorTmsSourceTest(unittest.TestCase):
    """`_mercator_raster_source` 产出的源,细节要优于同参数的双线性重投影。"""

    def test_source_beats_plain_bilinear(self):
        with TemporaryDirectory() as d:
            out = Path(d)
            _write_stripes(out / "任务_z14.tif")
            ctx = SimpleNamespace(task={"name": "任务"}, out_dir=out)

            got = _mercator_raster_source(ctx, 14)
            self.assertTrue(got.exists(), "未产出重投影源")

            ref = out / "ref.tif"
            shutil.copyfile(out / "任务_z14.tif", ref)
            reproject_geotiff(ref, "EPSG:4326", resampling=Resampling.bilinear)

            vg, vr = _lapvar(got), _lapvar(ref)
            self.assertGreater(
                vg, vr * 1.05,
                f"TMS 源仍在用低质量核(got={vg:.1f}, bilinear={vr:.1f})")

    def test_source_is_epsg4326(self):
        """回归:源必须是 4326 —— 否则切片枚举与源 bounds 求交集恒为空,
        TMS 阶段会报成功但一张都切不出来(该函数说明里记的坑)。"""
        with TemporaryDirectory() as d:
            out = Path(d)
            _write_stripes(out / "任务_z14.tif")
            ctx = SimpleNamespace(task={"name": "任务"}, out_dir=out)
            got = _mercator_raster_source(ctx, 14)
            with rasterio.open(got) as s:
                self.assertEqual(s.crs.to_string(), "EPSG:4326")


if __name__ == "__main__":
    unittest.main()
