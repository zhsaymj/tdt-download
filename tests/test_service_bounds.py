import json
import math
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.core.service_bounds import (
    bounds_from_tile_index, detect_grid, geodetic_bounds, mercator_bounds,
)


class GridConversionTest(unittest.TestCase):
    """三套网格不可混用。这些数值是实测基准，改动网格逻辑必然触发失败。"""

    def test_geodetic_uses_terrain_grid_not_tms_grid(self):
        # Cesium 地形网格：列 2^(L+1)、行 2^L、span 180/2^L
        # L=12 时 x=6120..6129, y=3063..3070 对应新疆矿_高程的真实数据
        b = geodetic_bounds(6120, 3063, 6129, 3070, 12)
        self.assertAlmostEqual(b[0], 88.945312, places=5)
        self.assertAlmostEqual(b[1], 44.604492, places=5)
        self.assertAlmostEqual(b[2], 89.384766, places=5)
        self.assertAlmostEqual(b[3], 44.956055, places=5)

    def test_image_tms_grid_is_different(self):
        """锁定两套 geodetic 网格不可混用：同一组瓦片号必须得到不同结果。"""
        from backend.core.tiling import tile_bounds
        terrain = geodetic_bounds(6120, 3063, 6129, 3070, 12)
        # 影像 TMS 的 span 是 360/2^z，terrain 是 180/2^L —— 数值必然不同
        tms_west, _, _, _ = tile_bounds(6120, 3063, 12)
        self.assertNotAlmostEqual(terrain[0], tms_west, places=3)

    def test_mercator_bounds(self):
        # Web 墨卡托 z=1：(0,0)-(1,1) 覆盖全球
        b = mercator_bounds(0, 0, 1, 1, 1)
        self.assertAlmostEqual(b[0], -180.0, places=4)
        self.assertAlmostEqual(b[1], -85.0511, places=3)
        self.assertAlmostEqual(b[2], 180.0, places=4)
        self.assertAlmostEqual(b[3], 85.0511, places=3)

    def test_multiline_range_is_not_degenerate(self):
        """多行区间必须得到有高度的矩形。

        两套网格的 y 都自北向南/自南向北各自单向，取错边会得到相邻瓦片的
        共边——四至退化成一个零高度矩形，**不报错，只是范围错**。
        单元素测试覆盖不到这个 bug，故单独锁一条。
        """
        # 墨卡托 y 自北向南：北边取 y_min、南边取 y_max
        b = mercator_bounds(0, 0, 1, 1, 1)
        self.assertLess(b[1], b[3])
        self.assertGreater(b[3] - b[1], 1.0)

        # 地形 y 自南向北：多行区间跨 4 行
        t = geodetic_bounds(6120, 3063, 6129, 3070, 12)
        self.assertLess(t[1], t[3])
        self.assertGreater(t[3] - t[1], 0.1)


class DetectGridTest(unittest.TestCase):
    def test_profile_attribute_wins(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tilemapresource.xml").write_text(
                '<TileMap><TileSets profile="geodetic">'
                '<TileSet href="0" order="0"/></TileSets></TileMap>',
                encoding="utf-8")
            self.assertEqual(detect_grid(p), "geodetic")

    def test_mercator_profile(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tilemapresource.xml").write_text(
                '<TileMap><TileSets profile="mercator">'
                '<TileSet href="0" order="0"/></TileSets></TileMap>',
                encoding="utf-8")
            self.assertEqual(detect_grid(p), "mercator")

    def test_z1_row_count_disambiguates(self):
        """无 XML 时看 z=1 的行数：geodetic 1 行、mercator 2 行。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "1" / "0").mkdir(parents=True)
            (p / "1" / "0" / "0.png").write_bytes(b"x")
            self.assertEqual(detect_grid(p), "geodetic")

        with TemporaryDirectory() as d:
            p = Path(d)
            for y in (0, 1):
                (p / "1" / "0").mkdir(parents=True, exist_ok=True)
                (p / "1" / "0" / f"{y}.png").write_bytes(b"x")
            self.assertEqual(detect_grid(p), "mercator")

    def test_defaults_to_mercator(self):
        with TemporaryDirectory() as d:
            self.assertEqual(detect_grid(Path(d)), "mercator")


class TileIndexBoundsTest(unittest.TestCase):
    def test_xyz_naming(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            for x in (2, 3):
                for y in (1, 2):
                    (p / "3" / str(x)).mkdir(parents=True, exist_ok=True)
                    (p / "3" / str(x) / f"{y}.png").write_bytes(b"x")
            b = bounds_from_tile_index(p, "mercator")
            self.assertIsNotNone(b)
            self.assertLess(b[0], b[2])   # west < east
            self.assertLess(b[1], b[3])   # south < north

    def test_col_row_naming(self):
        """本工具瓦片缓存格式 {z}/{col}_{row}.ext 也要认。"""
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "3").mkdir(parents=True)
            (p / "3" / "2_1.png").write_bytes(b"x")
            (p / "3" / "3_2.png").write_bytes(b"x")
            b = bounds_from_tile_index(p, "geodetic")
            self.assertIsNotNone(b)
            self.assertLess(b[0], b[2])

    def test_empty_dir_returns_none(self):
        with TemporaryDirectory() as d:
            self.assertIsNone(bounds_from_tile_index(Path(d), "mercator"))

    def test_max_files_marks_approx(self):
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "3" / "2").mkdir(parents=True)
            (p / "3" / "2" / "1.png").write_bytes(b"x")
            got = bounds_from_tile_index(p, "mercator", max_files=0)
            # 超限时用已扫到的部分，范围仍是四元组
            self.assertIsNotNone(got)


class MetadataBoundsTest(unittest.TestCase):
    def test_metadata_json(self):
        from backend.core.service_bounds import bounds_from_metadata
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "metadata.json").write_text(json.dumps({
                "bbox_wgs84": [100.0, 30.0, 101.0, 31.0]}), encoding="utf-8")
            self.assertEqual(bounds_from_metadata(p), [100.0, 30.0, 101.0, 31.0])

    def test_metadata_json_missing(self):
        from backend.core.service_bounds import bounds_from_metadata
        with TemporaryDirectory() as d:
            self.assertIsNone(bounds_from_metadata(Path(d)))

    def test_tilemapresource_attr_names_and_approx(self):
        """属性名是 minx/miny/maxx/maxy，且值是瓦片对齐四至 -> approx"""
        from backend.core.service_bounds import bounds_from_tilemapresource
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tilemapresource.xml").write_text(
                '<TileMap><BoundingBox miny="30.0" minx="100.0" '
                'maxy="31.0" maxx="101.0"/></TileMap>', encoding="utf-8")
            got = bounds_from_tilemapresource(p)
            self.assertEqual(got, ([100.0, 30.0, 101.0, 31.0], True))

    def test_layer_json_available_used_bounds_ignored(self):
        """layer.json 的 bounds 恒为世界范围，必须用 available 反算。"""
        from backend.core.service_bounds import bounds_from_layer_json
        with TemporaryDirectory() as d:
            p = Path(d)
            available = [[] for _ in range(13)]
            available[12] = [{"startX": 6120, "startY": 3063,
                              "endX": 6129, "endY": 3070}]
            (p / "layer.json").write_text(json.dumps({
                "bounds": [-180, -90, 180, 90],   # 世界范围，必须被忽略
                "available": available,
            }), encoding="utf-8")
            got = bounds_from_layer_json(p / "layer.json")
            self.assertIsNotNone(got)
            # 若误用 bounds 会得到 [-180,-90,180,90] —— 断言不是它
            self.assertNotAlmostEqual(got[0], -180.0, places=1)
            self.assertAlmostEqual(got[0], 88.945312, places=5)

    def test_tileset_region_form(self):
        """region 形态：弧度 -> 度"""
        from backend.core.service_bounds import bounds_from_tileset
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tileset.json").write_text(json.dumps({
                "root": {"boundingVolume": {"region": [
                    math.radians(100.0), math.radians(30.0),
                    math.radians(101.0), math.radians(31.0)]}}
            }), encoding="utf-8")
            got = bounds_from_tileset(p / "tileset.json")
            self.assertAlmostEqual(got[0], 100.0, places=5)
            self.assertAlmostEqual(got[3], 31.0, places=5)

    def test_tileset_box_form(self):
        """box 形态：中心 + 三个半轴向量（ECEF 米）"""
        from backend.core.service_bounds import bounds_from_tileset
        from pyproj import Transformer
        # 取一个真实经纬度，转 ECEF 后构造一个已知大小的 box
        tr = Transformer.from_crs("EPSG:4326", "EPSG:4978", always_xy=True)
        cx, cy, cz = tr.transform(100.0, 30.0, 0.0)
        tileset = {"root": {"boundingVolume": {"box": [
            cx, cy, cz,
            100.0, 0, 0,     # x 半轴（东向 100 米）
            0, 100.0, 0,     # y 半轴
            0, 0, 100.0,     # z 半轴
        ]}}}
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tileset.json").write_text(json.dumps(tileset), encoding="utf-8")
            got = bounds_from_tileset(p / "tileset.json")
            self.assertIsNotNone(got)
            self.assertLess(got[0], 100.0)
            self.assertGreater(got[2], 100.0)

    def test_tileset_unknown_form_returns_none(self):
        from backend.core.service_bounds import bounds_from_tileset
        with TemporaryDirectory() as d:
            p = Path(d)
            (p / "tileset.json").write_text(
                json.dumps({"root": {}}), encoding="utf-8")
            self.assertIsNone(bounds_from_tileset(p / "tileset.json"))


if __name__ == "__main__":
    unittest.main()
