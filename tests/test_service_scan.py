import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.core.service_scan import scan_dir


def _touch(p: Path, content: bytes = b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(content)


class ScanDirTest(unittest.TestCase):
    def test_detects_3dtiles(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "tileset.json",
                   json.dumps({"root": {"boundingVolume": {
                       "region": [0.1, 0.2, 0.3, 0.4]}}}).encode())
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "model")
            self.assertEqual(got[0].entry, "tileset.json")

    def test_detects_terrain(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "terrain" / "layer.json",
                   json.dumps({"available": [[{"startX": 0, "startY": 0,
                                               "endX": 0, "endY": 0}]]}).encode())
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "terrain")

    def test_detects_tms_dir(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "tms" / "tilemapresource.xml",
                   b'<TileMap><TileSets profile="geodetic"/></TileMap>')
            _touch(p / "tms" / "3" / "2" / "1.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "imagery")
            self.assertEqual(got[0].grid, "geodetic")
            self.assertTrue(got[0].flip_y)

    def test_detects_bare_xyz_dir(self):
        """无 tilemapresource.xml 的裸 XYZ 目录也要认出来。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            for y in (0, 1):
                _touch(p / "osm" / "1" / "0" / f"{y}.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0].kind, "imagery")
            self.assertEqual(got[0].grid, "mercator")
            self.assertFalse(got[0].flip_y)

    def test_detects_vector(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "范围.geojson", b'{"type":"FeatureCollection","features":[]}')
            got = scan_dir(p)
            kinds = {c.kind for c in got}
            self.assertIn("vector", kinds)

    def test_multiple_results_in_one_dir(self):
        """一个成果目录里可能同时有 3dtiles / terrain / 矢量，全部列出。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "tileset.json", b'{"root":{}}')
            _touch(p / "terrain" / "layer.json", b'{"available":[]}')
            _touch(p / "out.geojson", b'{"type":"FeatureCollection","features":[]}')
            got = scan_dir(p)
            self.assertEqual(len(got), 3)
            self.assertEqual({c.kind for c in got}, {"model", "terrain", "vector"})

    def test_ignores_empty_dir(self):
        with TemporaryDirectory() as d:
            self.assertEqual(scan_dir(Path(d)), [])

    def test_ignores_internal_files(self):
        """以下划线开头的中间文件不算成果。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "_temp.geojson", b'{"type":"FeatureCollection","features":[]}')
            self.assertEqual(scan_dir(p), [])

    def test_candidate_label_is_readable(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "tileset.json", b'{"root":{}}')
            got = scan_dir(p)
            self.assertTrue(got[0].label)
            self.assertNotIn("\\", got[0].label)

    def test_incomplete_3dtiles_is_not_a_candidate(self):
        """只有 b3dm 没有 tileset.json 的半成品目录不算成果。

        实测 output/OSGB/3dtiles/ 有 3.7 万个 b3dm 却没有 tileset.json
        （转换中断的残留）。若判据写成"有 b3dm 文件"，用户会发布一个
        加载失败的服务。
        """
        with TemporaryDirectory() as d:
            p = Path(d)
            _touch(p / "3dtiles" / "0" / "0" / "0.b3dm", b"x")
            _touch(p / "3dtiles" / "1" / "0" / "0.b3dm", b"x")
            self.assertEqual(scan_dir(p), [])

    def test_tile_dir_not_duplicated_across_levels(self):
        """同一份瓦片数据只识别一次。

        {z}/{x}/{y}.png 结构下 tms 与 tms/3 都"下方有瓦片"，只看这一点会
        重复识别成两个服务（实测发生过）。判据改为从瓦片文件反推根。
        """
        with TemporaryDirectory() as d:
            p = Path(d)
            for z in (2, 3):
                for x in (1, 2):
                    _touch(p / "tms" / str(z) / str(x) / "1.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1)
            self.assertEqual(Path(got[0].root).name, "tms")
            self.assertEqual((got[0].minzoom, got[0].maxzoom), (2, 3))


if __name__ == "__main__":
    unittest.main()
