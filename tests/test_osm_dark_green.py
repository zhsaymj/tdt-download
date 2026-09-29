"""OSM 切片:不得把「R 通道为 0 的真实像素」当成未覆盖(需求38)。

背景:`mosaic_to_geotiff` 产出的是 **3 波段无 alpha** 的 RGB,未覆盖区是画布上的
**全零**像素(该函数里"保持黑色填充,与原全图 canvas 语义一致"的注释就是这条约定)。

而 `osm._warped_vrt_kwargs` 给 WarpedVRT 设了 `nodata=0`,渲染时又拿
`vrt.read_masks(1)` 当 alpha —— GDAL 的 nodata 判定是**逐波段**的,只按第 1 波段
(R) 判,于是**只要 R 为 0 就透明**。深绿植被的 R 常为 0,成果里就出现一片片透明
麻点(用户截图:OSM 瓦片在绿色很深的地方变透明,而 COG 没有这个情况)。

只在**裁剪任务**上出现:那时 `_stage_osm` 不能复用带 alpha 的 `{name}_z{z}.tif`
(它被裁过),只能自拼这个 3 波段源,守卫 `_has_explicit_alpha_or_mask` 于是失效。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
import rasterio
from rasterio.transform import from_bounds

from backend.core.osm import export_osm

#: 正好跨 2 个 z12 瓦片(360/2^12 = 0.087890625)
_STEP = 360 / 2 ** 12
_BBOX = (0.0, 0.0, 2 * _STEP, _STEP)


def _write_source(path: Path) -> None:
    """3 波段、无 alpha 的 4326 源:左半是 R=0 的深绿(真实像素),右半全零(未覆盖)。

    R=0 是深绿植被的常见取值 —— 正是被误判成未覆盖的那种像素。

    profile 刻意与 `mosaic_to_geotiff` 的产物一致(photometric=RGB / LZW / 分块):
    GDAL 对"无 alpha 的 3 波段栅格"是否按 nodata 生成掩膜,取决于文件的 photometric
    等属性 —— 不照抄的话测出来的掩膜行为与线上不一致(实测踩到:漏掉 photometric
    时 read_masks 全返回 255,测试变成真空通过)。
    """
    w, h = 128, 64
    arr = np.zeros((3, h, w), dtype=np.uint8)
    arr[0, :, :w // 2] = 0      # R = 0  ← 触发误判
    arr[1, :, :w // 2] = 80     # G
    arr[2, :, :w // 2] = 30     # B
    # 右半保持全零 = 未覆盖
    with rasterio.open(path, "w", driver="GTiff", height=h, width=w, count=3,
                       dtype="uint8", crs="EPSG:4326", photometric="RGB",
                       compress="LZW", tiled=True,
                       blockxsize=64, blockysize=64, BIGTIFF="YES",
                       transform=from_bounds(*_BBOX, w, h)) as dst:
        dst.write(arr)


def _tile_xy(lon: float, lat: float, z: int = 12) -> tuple[int, int]:
    import math
    n = 2 ** z
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(math.radians(lat))
                            + 1 / math.cos(math.radians(lat))) / math.pi) / 2.0 * n)
    return x, y


class OsmDarkGreenTest(unittest.TestCase):
    def test_dark_green_pixels_are_not_treated_as_uncovered(self):
        """★ 核心 ★ 左半(R=0 的深绿)必须照常出图且不透明。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.tif"
            _write_source(src)
            out = root / "osm"
            export_osm(src, [12], _BBOX, out, concurrency=1)

            # 左半覆盖的那张瓦片:内容全是 R=0 的深绿,必须写出且完全不透明
            x, y = _tile_xy(_STEP / 2, _STEP / 2)
            tile = out / "12" / str(x) / f"{y}.png"
            self.assertTrue(
                tile.exists(),
                f"R=0 的深绿瓦片整张被判成空、直接跳过(未写出 {tile}) —— "
                f"说明仍按'第 1 波段为 0 即未覆盖'判定")
            with rasterio.open(tile) as s:
                alpha = s.read(s.count)
            self.assertEqual(int(alpha.min()), 255,
                             "深绿瓦片里出现了透明像素 —— R=0 被误判成未覆盖")
            self.assertEqual(int(alpha.max()), 255, "深绿瓦片应完全不透明")

    def test_tile_outside_source_is_skipped(self):
        """回归:源覆盖范围之外的瓦片仍要被跳过 —— 别把"放宽 alpha 判据"修成
        "什么都当有数据",那会让空白瓦片盖住底图。

        用"完全在源之外"的瓦片来测:它走的是 ix1<=ix0 那条确定性分支,不依赖
        GDAL 对合成文件的掩膜机制(那部分在合成图上与线上不一致,测不准)。
        """
        with TemporaryDirectory() as d:
            root = Path(d)
            src = root / "src.tif"
            _write_source(src)
            out = root / "osm"
            # bbox 只取源本身;瓦片范围则放到源以西 1° 处
            far_bbox = (_BBOX[0] - 1.0, _BBOX[1], _BBOX[2] - 1.0, _BBOX[3])
            export_osm(src, [12], far_bbox, out, concurrency=1)

            x, y = _tile_xy(_BBOX[0] - 0.5, _STEP / 2)
            tile = out / "12" / str(x) / f"{y}.png"
            self.assertFalse(
                tile.exists(),
                "源覆盖范围之外的瓦片被写出了 —— 空白区会盖住底图")


if __name__ == "__main__":
    unittest.main()
