"""批次2 内存优化的防回归护栏。

背景:`docs/内存审计报告.md` 的 #18/#19/#20/#23/#24/#29/#33/#34 八处。
共同性质是「在**主进程**里按要素数/目录数累积」——主进程一挂,连进度推送和
取消接口都一起没了。优化后要锁住三条性质:

  * 服务扫描不再为每个目录缓存文件清单(只留 has_tile / tile_ext 两个标量);
  * 建筑去重表与矢量转换都内存有界,且**语义与整份物化时完全一致**;
  * 点云清单是路径字符串而非 Path 对象,排序规则不变(001_ 前缀绑定不能漂)。

不用 output/ 里的真实成果做输入——那些随时会被清理,换台机器就失败。
"""
import json
import os
import tempfile
import unittest
import unittest.mock
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from backend.core import service_bounds as sb_mod
from backend.core.building_cache import BuildingCache
from backend.core.service_scan import _Tree


def _has_pyogrio() -> bool:
    try:
        import pyogrio  # noqa: F401
        return True
    except Exception:
        return False


def _tmp_seen_dbs() -> set:
    return set(Path(tempfile.gettempdir()).glob("bldg_seen_*.db"))


# ---------------------------------------------------------------- #23 服务扫描


class ServiceScanTreeTest(unittest.TestCase):
    def test_no_per_dir_file_cache(self):
        """_Tree 不再持有 children(每目录的文件 Path 列表)。

        9.2 万个文件的 Path 列表约 25 MB,是「扫描服务」接口在主进程里最大的一块。
        """
        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "任务A" / "0").mkdir(parents=True)
            (root / "任务A" / "0" / "1_1.png").write_bytes(b"x")
            tree = _Tree(root, 4)
            self.assertFalse(hasattr(tree, "children"),
                             "_Tree 不应再缓存每目录的文件列表")

    def test_files_lists_dir_without_caching(self):
        with TemporaryDirectory() as td:
            root = Path(td)
            d = root / "a"
            d.mkdir()
            (d / "1_1.png").write_bytes(b"x")
            (d / "2_2.jpg").write_bytes(b"x")
            tree = _Tree(root, 4)
            first = tree.files(d)
            second = tree.files(d)
            self.assertEqual(len(first), 2)
            self.assertIsNot(first, second, "files() 应为每次现扫,不缓存")

    def test_tile_roots_still_detected(self):
        """去掉 children 之后,tile_roots 仍要认出「直接含瓦片」的目录。"""
        with TemporaryDirectory() as td:
            root = Path(td)
            tiles = root / "任务B" / "tiles"
            tiles.mkdir(parents=True)
            for z in (10, 11):
                (tiles / str(z)).mkdir()
                (tiles / str(z) / "123_456.png").write_bytes(b"x")
            tree = _Tree(root, 4)
            self.assertIn(tiles, tree.tile_roots())


# ---------------------------------------------------------------- #24 3D 清单


class ServiceBoundsTest(unittest.TestCase):
    def _write_tileset(self, path: Path, region, n_tiles: int) -> None:
        children = [{"geometricError": 0,
                     "boundingVolume": {"region": [0, 0, 0.1, 0.1, 0, 1]},
                     "content": {"uri": f"{i}.b3dm"}} for i in range(n_tiles)]
        path.write_text(json.dumps({
            "asset": {"version": "1.0"},
            "geometricError": 500,
            "root": {"boundingVolume": {"region": region},
                     "geometricError": 0, "children": children},
        }), encoding="utf-8")

    def test_small_tileset_reads_inline(self):
        with TemporaryDirectory() as td:
            p = Path(td) / "tileset.json"
            self._write_tileset(p, [-2.0, 0.5, -1.0, 1.5], 2)
            got = sb_mod.bounds_from_tileset(p)
            self.assertIsNotNone(got)
            self.assertEqual(len(got), 4)

    def test_large_tileset_streams_and_matches(self):
        """超过内联阈值时走流式正则,结果必须与整份 json.loads 一致。"""
        with TemporaryDirectory() as td:
            p = Path(td) / "tileset.json"
            region = [-2.0, 0.5, -1.0, 1.5]
            self._write_tileset(p, region, 400)
            inline = sb_mod.bounds_from_tileset(p)
            self.assertIsNotNone(inline)
            # 阈值压到 1 字节,强制走流式分支
            with unittest.mock.patch.object(sb_mod, "_TILESET_INLINE_MAX", 1):
                streamed = sb_mod.bounds_from_tileset(p)
            self.assertIsNotNone(streamed)
            for a, b in zip(inline, streamed):
                self.assertAlmostEqual(a, b, places=9)


# ---------------------------------------------------------------- #29 缓存清空


class BuildingCacheClearTest(unittest.TestCase):
    def test_clear_removes_only_cache_fragments(self):
        with TemporaryDirectory() as td:
            tmp = Path(td)
            cache = BuildingCache(source_key="guard_test")
            cache.dir = tmp
            (tmp / "a.geojson.gz").write_bytes(b"x")
            (tmp / "b.geojson.gz").write_bytes(b"x")
            keep = tmp / "keep.txt"
            keep.write_text("x", encoding="utf-8")
            n = cache.clear()
            self.assertEqual(n, 2)
            self.assertTrue(keep.exists(), "非缓存文件不能被清掉")
            self.assertEqual(list(tmp.glob("*.geojson.gz")), [])


# ---------------------------------------------------------------- #20 跨格去重


class CachedSourceDedupTest(unittest.TestCase):
    def test_dedup_across_cells_uses_temp_db_and_cleans_it(self):
        """去重改用临时 sqlite:语义仍是「全 bbox 出现过就跳过」,但内存不随要素数涨。"""
        from backend.providers.buildings import BuildingFeature
        from backend.providers.cached import CachedBuildingSource

        same = BuildingFeature(
            fid="same-1",
            rings=[[(0.0, 0.0), (0.001, 0.0), (0.001, 0.001), (0.0, 0.001)]])

        class _Inner:
            key = "osm"
            label = "OSM"

            def fetch(self, bbox, *, on_progress=None, should_stop=None):
                yield same

        with TemporaryDirectory() as td:
            tmp = Path(td)
            src = CachedBuildingSource(_Inner(), cache_key="guard_test",
                                       use_remote=False)
            src.cache.dir = tmp
            before = _tmp_seen_dbs()
            out = list(src.fetch((0.0, 0.0, 2.0, 2.0)))
            after = _tmp_seen_dbs()
            self.assertEqual(after - before, set(), "临时去重库必须清理掉")
        self.assertEqual(len(out), 1, "同一 fid 的建筑只能输出一次")


# ---------------------------------------------------------------- #33/#34 清单


class LasFileListTest(unittest.TestCase):
    def test_returns_strings_sorted_like_paths(self):
        from backend.core import runner_3d

        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "b_dir").mkdir()
            (root / "A.las").write_bytes(b"x")
            (root / "b_dir" / "c.laz").write_bytes(b"x")
            (root / "b_dir" / "ignore.txt").write_bytes(b"x")
            got = runner_3d._las_files({"source_path": str(root)})
            self.assertTrue(all(isinstance(x, str) for x in got),
                            "清单必须是字符串,避免上万个 Path 对象")
            self.assertEqual(len(got), 2)
            self.assertEqual(got, sorted(got, key=os.path.normcase))

    def test_prefix_still_bound_to_first_file(self):
        """001_ 子目录取的是排序后的首个文件,改成字符串后绑定不能漂。"""
        from backend.core import runner_3d

        with TemporaryDirectory() as td:
            root = Path(td)
            (root / "x").mkdir()
            (root / "x" / "aaa.las").write_bytes(b"x")
            (root / "x" / "zzz.las").write_bytes(b"x")
            task = {"source_path": str(root), "name": "t", "id": "tid",
                    "output_path": str(root / "out"),
                    "provider": "local_pointcloud"}
            got = runner_3d.tiles3d_output(task)
            self.assertIn("001_aaa", str(got))


# ---------------------------------------------------------------- #19 矢量分批


@unittest.skipUnless(_has_pyogrio(), "pyogrio 不可用")
class VectorBatchTest(unittest.TestCase):
    def _make_gpkg(self, path: Path, n: int) -> None:
        import shapely
        from pyogrio.raw import write

        geoms = np.array(
            [shapely.to_wkb(shapely.box(i, i, i + 0.5, i + 0.5)) for i in range(n)],
            dtype=object)
        fields = np.asarray(["name"])
        data = [np.asarray([f"f{i}" for i in range(n)])]
        write(str(path), geoms, data, fields, driver="GPKG",
              geometry_type="Polygon", crs="EPSG:4326")

    def test_convert_vector_batches_keep_all_features(self):
        import pyogrio
        from backend.core import local_vector_file as lvf

        with TemporaryDirectory() as td:
            src = Path(td) / "src.gpkg"
            self._make_gpkg(src, 5)
            dst = Path(td) / "out.shp"
            with unittest.mock.patch.object(lvf, "_VECTOR_BATCH", 2):
                lvf.convert_vector(src, dst, "shapefile", to_wgs84=True)
            _m, _i, geoms, data = pyogrio.raw.read(str(dst))
            self.assertEqual(len(geoms), 5, "分批 append 不能丢要素")
            self.assertEqual(list(data[0]), [f"f{i}" for i in range(5)])

    def test_inspect_vector_uses_total_bounds(self):
        from backend.core.local_vector_file import inspect_vector

        with TemporaryDirectory() as td:
            src = Path(td) / "src.gpkg"
            self._make_gpkg(src, 3)
            info = inspect_vector(src)
            self.assertEqual(info["features"], 3)
            self.assertIsNotNone(info["bounds_wgs84"],
                                 "范围应来自 read_info 的 total_bounds")

    def test_task_vector_streams_multiple_batches(self):
        """#18:成果转 GeoJSON 分批读、流式出,拼起来仍是合法 FeatureCollection。"""
        import asyncio

        from backend.api import tasks as tmod

        with TemporaryDirectory() as td:
            out = Path(td)
            self._make_gpkg(out / "a.gpkg", 5)
            task = {"id": "t1", "name": "n", "output_path": str(out)}

            async def _run():
                resp = await tmod.api_task_vector("t1", "a.gpkg")
                chunks = []
                async for c in resp.body_iterator:
                    chunks.append(c if isinstance(c, bytes) else c.encode())
                return b"".join(chunks)

            with unittest.mock.patch.object(tmod, "get_task", return_value=task), \
                 unittest.mock.patch.object(tmod, "_VECTOR_GEOJSON_BATCH", 2):
                body = asyncio.run(_run())

        doc = json.loads(body)
        self.assertEqual(doc["type"], "FeatureCollection")
        self.assertEqual(len(doc["features"]), 5, "分批不能丢要素")
        self.assertEqual([f["properties"]["name"] for f in doc["features"]],
                         [f"f{i}" for i in range(5)])


if __name__ == "__main__":
    unittest.main()
