"""三维数据任务的成果展示:体积明细归类/标签(api.tasks._scan_output_size)
与叠加清单的 3dtiles 标签及 tileset url(core.overlay.list_layers)。
"""
import tempfile
import unittest
from pathlib import Path


def _touch(p: Path, size: int = 4) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)


def _items_by_key(result: dict) -> dict:
    return {it["key"]: it for it in result["items"]}


class ScanOutputSize3dTest(unittest.TestCase):
    """点云 DSM 不计入影像 GeoTIFF;DEM 与 3dtiles 的标签随 provider 区分。"""

    def _scan(self, task: dict) -> dict:
        from backend.api.tasks import _scan_output_size
        return _scan_output_size(task)

    def test_pointcloud_dsm_single_bucket_and_dem_label(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _touch(out / "点云A_dsm.tif", 10)
            _touch(out / "点云A_dem.tif", 20)
            task = {"name": "点云A", "provider": "local_pointcloud",
                    "output_path": str(out)}
            items = _items_by_key(self._scan(task))
            # dsm 不是影像,不能落进 geotiff 桶标成「影像 GeoTIFF」
            self.assertNotIn("geotiff", items)
            self.assertEqual(items["pc_dsm"]["label"], "DSM GeoTIFF")
            self.assertEqual(items["pc_dsm"]["bytes"], 10)
            # 点云的 {name}_dem.tif 是 DEM(仅地面点),不是建筑地面高程
            self.assertEqual(items["bld_dem"]["label"], "DEM GeoTIFF")
            self.assertEqual(items["bld_dem"]["bytes"], 20)

    def test_buildings_dem_label_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _touch(out / "白模_dem.tif", 20)
            task = {"name": "白模", "provider": "osm_buildings",
                    "output_path": str(out)}
            items = _items_by_key(self._scan(task))
            self.assertEqual(items["bld_dem"]["label"], "地面高程 GeoTIFF")
            self.assertNotIn("pc_dsm", items)

    def test_3dtiles_label_by_provider(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _touch(out / "3dtiles" / "tileset.json", 8)
            for provider, label in (("local_osgb", "3D Tiles 瓦片集"),
                                    ("local_pointcloud", "3D Tiles 瓦片集"),
                                    ("osm_buildings", "三维建筑 3D Tiles")):
                with self.subTest(provider=provider):
                    task = {"name": "m", "provider": provider,
                            "output_path": str(out)}
                    items = _items_by_key(self._scan(task))
                    self.assertEqual(items["b3dm"]["label"], label)

    def test_image_task_dsm_like_name_still_image_geotiff(self):
        # 影像任务的普通 {name}_z*.tif 归类不受三维分支影响
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            _touch(out / "影像_z13.tif", 30)
            task = {"name": "影像", "provider": "tianditu_img",
                    "output_path": str(out)}
            items = _items_by_key(self._scan(task))
            self.assertEqual(items["geotiff"]["label"], "影像 GeoTIFF")
            self.assertEqual(items["geotiff"]["bytes"], 30)


class ListLayers3dTest(unittest.TestCase):
    """3dtiles 叠加项:label 随 provider;url 定位主 tileset.json(含多文件点云子目录)。"""

    def _layers(self, task: dict, out_dir: Path) -> list:
        from backend.core.overlay import list_layers
        return list_layers(task, out_dir.parent)

    def _p3d(self, layers: list) -> dict:
        return next(L for L in layers if L["id"] == "p3d_3dtiles")

    def test_osgb_label_and_root_tileset_url(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "任务A"
            _touch(out / "3dtiles" / "tileset.json")
            layers = self._layers(
                {"id": 1, "name": "任务A", "provider": "local_osgb",
                 "output_path": str(out)}, out)
            item = self._p3d(layers)
            self.assertEqual(item["label"], "倾斜模型 3D Tiles")
            self.assertTrue(item["url"].endswith("/3dtiles/tileset.json"))

    def test_pointcloud_multifile_uses_first_subdir_tileset(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "点云B"
            _touch(out / "3dtiles" / "002_b" / "tileset.json")
            _touch(out / "3dtiles" / "001_a" / "tileset.json")
            layers = self._layers(
                {"id": 2, "name": "点云B", "provider": "local_pointcloud",
                 "output_path": str(out)}, out)
            item = self._p3d(layers)
            self.assertEqual(item["label"], "点云 3D Tiles")
            # 多文件点云主产物取第一个文件的子目录(与 runner_3d.tiles3d_output 一致)
            self.assertTrue(item["url"].endswith("/3dtiles/001_a/tileset.json"))

    def test_buildings_label_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "白模"
            _touch(out / "3dtiles" / "tileset.json")
            layers = self._layers(
                {"id": 3, "name": "白模", "provider": "osm_buildings",
                 "output_path": str(out)}, out)
            item = self._p3d(layers)
            self.assertEqual(item["label"], "三维建筑白模(b3dm)")
            self.assertTrue(item["url"].endswith("/3dtiles/tileset.json"))

    def test_no_tileset_json_then_no_url(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "空"
            (out / "3dtiles").mkdir(parents=True)
            layers = self._layers(
                {"id": 4, "name": "空", "provider": "local_osgb",
                 "output_path": str(out)}, out)
            item = self._p3d(layers)
            self.assertNotIn("url", item)


if __name__ == "__main__":
    unittest.main()
