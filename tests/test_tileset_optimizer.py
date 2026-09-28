import json
import tempfile
import unittest
from pathlib import Path

from backend.core.tileset_optimizer import optimize_tileset


def _node(uri, children=None, error=1):
    node = {
        "boundingVolume": {"box": [0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1]},
        "geometricError": error,
        "refine": "REPLACE",
        "content": {"uri": uri},
    }
    if children:
        node["children"] = children
    return node


class TilesetOptimizerTest(unittest.TestCase):
    def test_externalizes_tile_subtree_and_preserves_pyramid(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tile = root / "Data" / "Tile_+001_+002"
            tile.mkdir(parents=True)
            (tile / "coarse.b3dm").write_bytes(b"coarse")
            (tile / "fine.b3dm").write_bytes(b"fine")
            pyramid = root / "Data" / "_pyramid"
            pyramid.mkdir()
            (pyramid / "r.b3dm").write_bytes(b"pyramid")
            data = {
                "asset": {"version": "1.0"},
                "geometricError": 8,
                "root": {
                    "boundingVolume": _node("x")["boundingVolume"],
                    "geometricError": 8,
                    "refine": "REPLACE",
                    "children": [_node("./Data/_pyramid/r.b3dm", [
                        _node("./Data/Tile_+001_+002/coarse.b3dm", [
                            _node("./Data/Tile_+001_+002/fine.b3dm", error=0)
                        ], error=2)
                    ], error=4)],
                },
            }
            manifest = root / "tileset.json"
            manifest.write_text(json.dumps(data), encoding="utf-8")

            result = optimize_tileset(manifest)

            updated = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(result.external_count, 1)
            self.assertEqual(updated["root"]["children"][0]["content"]["uri"],
                             "./Data/_pyramid/r.b3dm")
            proxy = updated["root"]["children"][0]["children"][0]
            self.assertEqual(proxy["content"]["uri"],
                             "./Data/Tile_+001_+002/tileset.json")
            self.assertNotIn("children", proxy)
            external = json.loads((tile / "tileset.json").read_text(encoding="utf-8"))
            self.assertEqual(external["root"]["content"]["uri"], "./coarse.b3dm")
            self.assertEqual(external["root"]["children"][0]["content"]["uri"],
                             "./fine.b3dm")

    def test_normalizes_error_like_reference_hierarchy(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tile = root / "Data" / "Tile_001"
            tile.mkdir(parents=True)
            (tile / "coarse.b3dm").write_bytes(b"coarse")
            (tile / "fine.b3dm").write_bytes(b"fine")
            pyramid = root / "Data" / "_pyramid"
            pyramid.mkdir()
            (pyramid / "r.b3dm").write_bytes(b"pyramid")
            box = {"box": [0, 0, 0, 270, 0, 0, 0, 223, 0, 0, 0, 23]}
            child = _node("./Data/Tile_001/coarse.b3dm", [
                _node("./Data/Tile_001/fine.b3dm", error=80)
            ], error=160)
            child["boundingVolume"] = box
            top = _node("./Data/_pyramid/r.b3dm", [child], error=23000)
            top["boundingVolume"] = box
            manifest = root / "tileset.json"
            manifest.write_text(json.dumps({
                "asset": {"version": "1.0"}, "geometricError": 46000,
                "root": {"boundingVolume": box, "geometricError": 46000,
                         "refine": "REPLACE", "children": [top]},
            }), encoding="utf-8")

            optimize_tileset(manifest)

            updated = json.loads(manifest.read_text(encoding="utf-8"))
            root_error = updated["root"]["geometricError"]
            self.assertAlmostEqual(root_error, (540 ** 2 + 446 ** 2) ** 0.5 / 2)
            self.assertAlmostEqual(updated["root"]["children"][0]["geometricError"],
                                   540 / 144)
            proxy = updated["root"]["children"][0]["children"][0]
            self.assertAlmostEqual(proxy["geometricError"], 540 / 288)
            external = json.loads((tile / "tileset.json").read_text(encoding="utf-8"))
            self.assertAlmostEqual(external["root"]["geometricError"], 540 / 288)
            self.assertAlmostEqual(external["root"]["children"][0]["geometricError"],
                                   540 / 576)

    def test_rejects_missing_content_without_changing_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest = root / "tileset.json"
            original = json.dumps({
                "asset": {"version": "1.0"},
                "geometricError": 2,
                "root": {"boundingVolume": _node("x")["boundingVolume"],
                         "geometricError": 2, "refine": "REPLACE",
                         "children": [_node("./Data/Tile_+001_+002/missing.b3dm")]},
            })
            manifest.write_text(original, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "不存在"):
                optimize_tileset(manifest)
            self.assertEqual(manifest.read_text(encoding="utf-8"), original)

    def test_prunes_missing_content_but_keeps_valid_descendants(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tile = root / "Data" / "Tile_001"
            tile.mkdir(parents=True)
            (tile / "fine.b3dm").write_bytes(b"fine")
            manifest = root / "tileset.json"
            manifest.write_text(json.dumps({
                "asset": {"version": "1.0"}, "geometricError": 4,
                "root": {"boundingVolume": _node("x")["boundingVolume"],
                         "geometricError": 4, "refine": "REPLACE",
                         "children": [_node("./Data/Tile_001/missing.b3dm", [
                             _node("./Data/Tile_001/fine.b3dm", error=0)
                         ], error=2)]},
            }), encoding="utf-8")

            result = optimize_tileset(manifest)

            self.assertEqual(result.missing_count, 1)
            external = json.loads((tile / "tileset.json").read_text(encoding="utf-8"))
            self.assertNotIn("content", external["root"])
            self.assertEqual(external["root"]["children"][0]["content"]["uri"],
                             "./fine.b3dm")

    def test_second_run_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tile = root / "Data" / "Tile_+001_+002"
            tile.mkdir(parents=True)
            (tile / "model.b3dm").write_bytes(b"model")
            manifest = root / "tileset.json"
            manifest.write_text(json.dumps({
                "asset": {"version": "1.0"},
                "geometricError": 2,
                "root": {"boundingVolume": _node("x")["boundingVolume"],
                         "geometricError": 2, "refine": "REPLACE",
                         "children": [_node("./Data/Tile_+001_+002/model.b3dm")]},
            }), encoding="utf-8")
            optimize_tileset(manifest)
            before = manifest.read_bytes()
            result = optimize_tileset(manifest)
            self.assertEqual(result.external_count, 0)
            self.assertEqual(manifest.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
