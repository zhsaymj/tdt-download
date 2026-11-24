"""批次3 内存优化的防回归护栏。

每条用例对应 `docs/内存审计报告.md` 里的一处风险项,断言的都不是"实现长什么样",
而是"峰值不随规模增长"这条性质本身:

    #11  GDAL_CACHEMAX 有显式上限
    #13/#14  产物校验只看关键文件,不再把全部瓦片列成 list
    #21  转容器分批追加,结果要素数与源文件一致
    #27  外部 CLI 子进程能拿到 GDAL_CACHEMAX
    #30  worker 数有上界
    #35  外部化清单边算边落盘,不留临时文件
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.core import runner_buildings as rb
from backend.core import tileset_optimizer as opt
from backend.core.processors.base import run_cli
from backend.core.processors.las_to_3dtiles import LasTo3dTiles
from backend.core.processors.osgb_to_3dtiles import OsgbTo3dTiles


class ExpectedOutputsLazyTest(unittest.TestCase):
    """#14:瓦片数再多,expected_outputs 也只返回关键文件。"""

    def test_osgb_many_tiles_still_one_output(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "tileset.json").write_text("{}", encoding="utf-8")
            for i in range(50):
                sub = out / "Data" / f"Tile_{i:03d}_000"
                sub.mkdir(parents=True)
                (sub / f"Tile_{i:03d}_000.b3dm").write_bytes(b"b3dm")
            self.assertEqual(p.expected_outputs(out), [out / "tileset.json"])

    def test_las_many_tiles_still_one_output(self):
        p = LasTo3dTiles(python="x")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "tileset.json").write_text("{}", encoding="utf-8")
            points = out / "points"
            points.mkdir()
            for i in range(50):
                (points / f"r{i}.pnts").write_bytes(b"pnts")
            self.assertEqual(p.expected_outputs(out), [out / "tileset.json"])


class ClampWorkersTest(unittest.TestCase):
    """#30:worker 数上界,防止"内存上限 ÷ worker 数"被无限稀释。"""

    def test_zero_and_negative_fall_to_one(self):
        from backend.config import _clamp_workers
        self.assertEqual(_clamp_workers(0), 1)
        self.assertEqual(_clamp_workers(-3), 1)

    def test_upper_bound(self):
        from backend.config import WorkerConfig, _clamp_workers
        self.assertEqual(_clamp_workers(9999), WorkerConfig.MAX_WORKERS)

    def test_string_value(self):
        from backend.config import _clamp_workers
        self.assertEqual(_clamp_workers("4"), 4)

    def test_garbage_falls_back_to_configured(self):
        from backend.config import WorkerConfig, _clamp_workers
        self.assertEqual(_clamp_workers("abc"), WorkerConfig.num_workers)


class GdalCacheMaxTest(unittest.TestCase):
    """#11:块缓存有显式上限,不再按物理内存 5% 自动取。"""

    def test_env_is_set(self):
        from backend.core import gdal_env  # noqa: F401  导入即生效
        self.assertIn("GDAL_CACHEMAX", os.environ)
        self.assertEqual(int(os.environ["GDAL_CACHEMAX"]), 512)


class RunCliEnvTest(unittest.TestCase):
    """#27:外部工具没有内存参数,只能靠子进程环境兜住 GDAL 块缓存。"""

    def test_child_sees_cachemax(self):
        lines: list[str] = []
        code = "import os;print(os.environ.get('GDAL_CACHEMAX'))"
        run_cli([sys.executable, "-c", code], on_stdout_line=lines.append)
        self.assertEqual([x.strip() for x in lines if x.strip()], ["512"])


class TilesetOptimizerStreamingTest(unittest.TestCase):
    """#35:外部子树边校验边落临时文件,plans 只留路径对。"""

    @staticmethod
    def _write_tileset(root: Path) -> Path:
        tile_dir = root / "Data" / "Tile_000_000"
        tile_dir.mkdir(parents=True)
        (tile_dir / "Tile_000_000.b3dm").write_bytes(b"b3dm")
        box = [0, 0, 0, 100, 0, 0, 0, 100, 0, 0, 0, 100]
        data = {
            "asset": {"version": "1.0"},
            "geometricError": 200.0,
            "root": {
                "boundingVolume": {"box": box},
                "geometricError": 200.0,
                "children": [{
                    "boundingVolume": {"box": box},
                    "geometricError": 100.0,
                    "content": {"uri": "./Data/Tile_000_000/Tile_000_000.b3dm"},
                }],
            },
        }
        path = root / "tileset.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_externalizes_and_leaves_no_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = self._write_tileset(root)
            result = opt.optimize_tileset(path)
            self.assertEqual(result.external_count, 1)
            external = root / "Data" / "Tile_000_000" / "tileset.json"
            self.assertTrue(external.is_file())
            main = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(main["root"]["children"][0]["content"]["uri"],
                             "./Data/Tile_000_000/tileset.json")
            self.assertEqual(list(root.rglob("*.tmp")), [])

    def test_missing_content_pruned_without_tmp(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            path = self._write_tileset(root)
            (root / "Data" / "Tile_000_000" / "Tile_000_000.b3dm").unlink()
            # 唯一瓦片被剪掉 → 全无内容,按约定报错,且不留临时文件
            with self.assertRaises(ValueError):
                opt.optimize_tileset(path)
            self.assertEqual(list(root.rglob("*.tmp")), [])


class VectorConvertBatchTest(unittest.TestCase):
    """#21:分批读 + append 写,结果要素数与源文件一致。"""

    def test_batches_keep_all_features(self):
        try:
            import pyogrio
        except ImportError:                     # pragma: no cover
            self.skipTest("未安装 pyogrio")
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            src = root / "b.geojson"
            features = [{
                "type": "Feature",
                "properties": {"id": f"b{i}", "name": f"楼{i}", "height": 10.0,
                               "base_height": 0.0, "top_height": 10.0,
                               "height_src": "height"},
                "geometry": {"type": "Polygon", "coordinates": [[
                    [120.0 + i * 0.001, 30.0], [120.001 + i * 0.001, 30.0],
                    [120.001 + i * 0.001, 30.001], [120.0 + i * 0.001, 30.001],
                    [120.0 + i * 0.001, 30.0]]]},
            } for i in range(5)]
            src.write_text(json.dumps({"type": "FeatureCollection",
                                       "features": features}), encoding="utf-8")
            task = {"containers": {"fetch_buildings": "gpkg"}, "name": "t"}
            with mock.patch.object(rb, "_VECTOR_WRITE_BATCH", 2):
                out = rb._to_selected_container(task, src)
            self.assertEqual(out.suffix, ".gpkg")
            self.assertEqual(pyogrio.read_info(str(out))["features"], 5)

    def test_geojson_container_returns_source(self):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "b.geojson"
            src.write_text('{"type":"FeatureCollection","features":[]}',
                           encoding="utf-8")
            task = {"containers": {"fetch_buildings": "geojson"}, "name": "t"}
            self.assertEqual(rb._to_selected_container(task, src), src)


class GeoJsonFeatureIteratorTest(unittest.TestCase):
    """#16:增量解析 —— 读侧峰值与文件大小脱钩,结果与 json.loads 等价。"""

    def test_streams_across_chunk_boundary(self):
        features = [{"type": "Feature", "properties": {"id": str(i)},
                     "geometry": {"type": "Polygon",
                                  "coordinates": [[[120.0, 30.0]]]}}
                    for i in range(500)]
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v.geojson"
            path.write_text(json.dumps({"type": "FeatureCollection",
                                        "features": features}),
                            encoding="utf-8")
            # 块小到单个 feature 都放不下,强制走"喂更多数据"的分支
            got = list(rb._iter_geojson_features(path, chunk=64))
        self.assertEqual(got, features)

    def test_truncated_file_raises(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "v.geojson"
            path.write_text('{"type":"FeatureCollection","features":[{"a":1}',
                            encoding="utf-8")
            with self.assertRaises(ValueError):
                list(rb._iter_geojson_features(path, chunk=16))


class PbfShardConstantsTest(unittest.TestCase):
    """#17:分片缓冲有界,且分片阈值是模块常量而非隐式默认。"""

    def test_shard_flush_lines_defined(self):
        from backend.core import pbf_import
        self.assertGreater(pbf_import.SHARD_FLUSH_LINES, 0)
        self.assertLessEqual(pbf_import.SHARD_FLUSH_LINES, 100_000)


if __name__ == "__main__":
    unittest.main()
