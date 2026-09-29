"""OSM 自拼源的**范围与坐标系**必须按数据源自己的网格取(需求38-2)。

原先 `_stage_osm` 的自拼路径写死 `range_for_bbox`(4326 网格)且不传 `crs` ——
对墨卡托源(Google/Esri)会按 4326 的行列号去读按 3857 命名的瓦片缓存,一张都
命中不了,整幅拼成空白。常规情况下走的是"复用 geotiff 成果/留存源"分支,这条
自拼路径没被覆盖到,问题一直藏着。

墨卡托网格第 z 级有 2^z 行,4326 网格只有 2^(z-1) 行 —— 所以两者的行号必然不同,
用错网格就会全落空。本测试用真实缓存目录验证这一点。
"""
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np
import rasterio
from rasterio.transform import from_origin

import backend.core.runner as runner
from backend.core.formats import GEO_MERCATOR
from backend.core.mercator_tiling import mercator_range_for_bbox
from backend.core.runner import _build_provider_for, _osm_source_for_level

#: 一小块区域 + 低级别 → 瓦片数少,测试快
_BBOX = (114.2, 30.4, 114.6, 30.8)
_Z = 6


def _write_cache_tile(path: Path) -> None:
    """写一张非空的缓存瓦片(值 200),供拼接器读出非零内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    arr = np.full((3, 8, 8), 200, dtype=np.uint8)
    with rasterio.open(path, "w", driver="PNG", height=8, width=8, count=3,
                       dtype="uint8") as dst:
        dst.write(arr)


class OsmSourceGridTest(unittest.TestCase):
    def _ctx(self, out_dir: Path, task: dict, provider, cache: Path):
        """最小上下文。downloader 只需 tile_path(缓存路径规则,与真实实现一致)。"""
        return SimpleNamespace(
            task=task, out_dir=out_dir, provider=provider,
            grid=GEO_MERCATOR, bbox=_BBOX, geom=None,
            anno_downloader=None, should_stop=lambda: False,
            tracker=SimpleNamespace(update=lambda *a, **k: None),
            downloader=SimpleNamespace(
                tile_path=lambda col, row, z:
                    cache / provider.key / str(z) / f"{col}_{row}.{provider.ext}"),
        )

    def test_mercator_source_reads_mercator_cache(self):
        """★ 核心 ★ 自拼源要按 3857 的行列号读缓存,并产出 3857 栅格。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            cache = root / "tiles"
            out = root / "out"
            out.mkdir()
            provider = _build_provider_for({"provider": "google_img"})

            # 按**墨卡托**网格铺满该范围的缓存瓦片
            tr = mercator_range_for_bbox(*_BBOX, _Z)
            n = 0
            for col in range(tr.col_min, tr.col_max + 1):
                for row in range(tr.row_min, tr.row_max + 1):
                    _write_cache_tile(
                        cache / provider.key / str(_Z)
                        / f"{col}_{row}.{provider.ext}")
                    n += 1
            self.assertGreater(n, 0, "测试前提:该范围应至少有一张墨卡托瓦片")

            orig = runner.settings.download.cache_dir
            runner.settings.download.cache_dir = str(cache)
            try:
                got, reusable = _osm_source_for_level(
                    self._ctx(out, {"name": "任务", "provider": "google_img"},
                              provider, cache), _Z)
            finally:
                runner.settings.download.cache_dir = orig

            self.assertTrue(got.exists(), "未产出 OSM 源")
            self.assertFalse(reusable, "自拼的源应标记为可删")
            with rasterio.open(got) as s:
                self.assertEqual(s.crs.to_string(), "EPSG:3857",
                                 "墨卡托源应拼成 3857(下游 OSM 才不用再重投影)")
                data = s.read()
            self.assertGreater(int(data.max()), 0,
                               "拼出来是空的 —— 说明仍按 4326 的行列号找缓存,一张没命中")

    def test_reuses_kept_unclipped_source(self):
        """回归:裁剪前留存的未裁剪源存在时直接复用,不重拼。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            out = root / "out"
            out.mkdir()
            kept = out / f".osm_src_z{_Z}.tif"
            _write_cache_tile(kept)          # 内容无所谓,只要存在且非空
            provider = _build_provider_for({"provider": "google_img"})

            got, reusable = _osm_source_for_level(
                self._ctx(out, {"name": "任务", "provider": "google_img"},
                          provider, root / "tiles"), _Z)
            self.assertEqual(got, kept)
            self.assertTrue(reusable, "复用留存源时不该在切完后删掉它")


if __name__ == "__main__":
    unittest.main()
