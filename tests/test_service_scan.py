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

    def test_ignores_external_tile_tilesets_under_model_root(self):
        with TemporaryDirectory() as d:
            root = Path(d) / "3dtiles"
            _touch(root / "tileset.json", b'{"root":{}}')
            for name in ("Tile_001", "Tile_002"):
                _touch(root / "Data" / name / "tileset.json", b'{"root":{}}')
            got = scan_dir(root)
            self.assertEqual(len(got), 1)
            self.assertEqual(Path(got[0].root), root.resolve())
            tile_only = scan_dir(root / "Data" / "Tile_001")
            self.assertEqual(len(tile_only), 1)

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

    def test_detects_tms_under_task_dir(self):
        """★ 扫 output/ 根目录时,任务目录下的 tms/{z}/{x}/{y}.png 必须认出来。

        这是本工具**自己的导出布局**,也是"未注册的成果"列表的主要来源:
        扫描根是 output/,于是瓦片文件落在
        `output/<任务名>/tms/<z>/<x>/<y>.png` —— 比 MAX_DEPTH 注释里假设的
        "成果目录不超过 3 层(output/<任务名>/<成果类型>/<文件>)"深一层。

        实测(2026-09-29):MAX_DEPTH=3 时 output/ 下 6 个候选里影像 **0 个**,
        10 个已下载的影像成果全部漏掉;放到 4 就是 10 个。用户报的正是这个
        ——"下载的影像数据是否已经添加到未注册的成果列表"。
        """
        with TemporaryDirectory() as d:
            p = Path(d)
            task = p / "Google卫星影像_20260101_120000"
            for x in (3456, 3457):
                _touch(task / "tms" / "12" / str(x) / "789.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1, f"未识别出影像成果,实际:{got}")
            self.assertEqual(got[0].kind, "imagery")
            self.assertEqual(Path(got[0].root), (task / "tms").resolve())
            self.assertEqual((got[0].minzoom, got[0].maxzoom), (12, 12))

    def test_detects_col_row_layout_under_task_dir(self):
        """扁平命名 {z}/{col}_{row}.png 在任务目录下同样要认出来(回归护栏)。

        它在第 3 层,MAX_DEPTH=3 时就已能识别 —— 这条锁住加深度后别把它弄坏。
        """
        with TemporaryDirectory() as d:
            p = Path(d)
            task = p / "天地图影像_20260101"
            _touch(task / "tms" / "12" / "3456_789.png")
            got = scan_dir(p)
            self.assertEqual(len(got), 1, f"未识别出影像成果,实际:{got}")
            self.assertEqual(got[0].kind, "imagery")
            self.assertEqual(Path(got[0].root), (task / "tms").resolve())

    def test_task_dir_tms_and_vector_both_found(self):
        """一个任务目录同时有 tms 与 geojson 时,两个成果都要列出(不互相遮蔽)。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            task = p / "影像任务_20260101"
            _touch(task / "tms" / "12" / "3456" / "789.png")
            _touch(task / "范围.geojson",
                   b'{"type":"FeatureCollection","features":[]}')
            got = scan_dir(p)
            self.assertEqual({c.kind for c in got}, {"imagery", "vector"},
                             f"实际:{[(c.kind, c.root) for c in got]}")


if __name__ == "__main__":
    unittest.main()
