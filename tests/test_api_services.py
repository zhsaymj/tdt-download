import json
import shutil
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient


class ServiceApiTest(unittest.TestCase):
    def setUp(self):
        from backend import db
        # sqlite 连接帧引用会让 db 文件在 Windows 上被锁，清理失败不掩盖断言
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self._old = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(self._restore)
        db.init_db()
        from backend.main import app
        self.client = TestClient(app)
        self.data = Path(self._tmp.name) / "data"
        (self.data / "3" / "2").mkdir(parents=True)
        (self.data / "3" / "2" / "1.png").write_bytes(b"PNGDATA")

    def _restore(self):
        from backend import db
        db.DB_PATH = self._old

    def tearDown(self):
        pass

    def _register(self, enabled=True, **kw):
        from backend.core.service_registry import add_service
        base = {"name": "t", "kind": "imagery", "root": str(self.data),
                "grid": "mercator", "tile_ext": "png", "enabled": enabled}
        base.update(kw)
        return add_service(**base)

    def test_list_empty(self):
        r = self.client.get("/api/services")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["services"], [])

    def test_disabled_service_returns_403(self):
        sid = self._register(enabled=False)
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 403)

    def test_enabled_service_serves_file(self):
        sid = self._register()
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"PNGDATA")

    def test_missing_source_returns_410(self):
        sid = self._register()
        shutil.rmtree(self.data)
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 410)

    def test_path_traversal_blocked(self):
        sid = self._register()
        r = self.client.get(f"/api/svc/{sid}/../../etc/passwd")
        self.assertIn(r.status_code, (403, 404))

    def test_unknown_service_404(self):
        r = self.client.get("/api/svc/deadbeef/3/2/1.png")
        self.assertEqual(r.status_code, 404)

    def test_toggle_enabled(self):
        sid = self._register(enabled=False)
        r = self.client.patch(f"/api/services/{sid}", json={"enabled": True})
        self.assertEqual(r.status_code, 200)
        r = self.client.get(f"/api/svc/{sid}/3/2/1.png")
        self.assertEqual(r.status_code, 200)

    def test_remove_service(self):
        sid = self._register()
        r = self.client.delete(f"/api/services/{sid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.client.get("/api/services").json()["services"], [])

    def test_health_reports_missing(self):
        sid = self._register()
        shutil.rmtree(self.data)
        r = self.client.get("/api/services/health")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["health"][sid]["ok"])

    def test_scan_endpoint(self):
        r = self.client.post("/api/services/scan", json={"path": str(self.data)})
        self.assertEqual(r.status_code, 200)
        self.assertIn("candidates", r.json())

    def test_overlay_desc_has_bounds_for_every_kind(self):
        """瓦片也要有 bounds —— 现有 overlay.py 的 tiles 项不带，照抄会漏。"""
        sid = self._register(bounds_wgs84=[100.0, 30.0, 101.0, 31.0])
        got = self.client.get("/api/services").json()["services"]
        desc = got[0]["overlay_desc"]
        self.assertEqual(desc["kind"], "tiles")
        self.assertEqual(desc["bounds_wgs84"], [100.0, 30.0, 101.0, 31.0])

    def test_access_path_keeps_placeholders(self):
        """瓦片模板的 {z}{x}{y} 必须原样保留，不能被 URL 转义。"""
        self._register()
        got = self.client.get("/api/services").json()["services"]
        path = got[0]["access_path"]
        self.assertIn("{z}", path)
        self.assertIn("{x}", path)
        self.assertIn("{y}", path)

    def test_model_kind_maps_to_bbox_overlay(self):
        """模型/地形在二维地图上只画范围框，复用 raster_only_bbox 不引入新 kind。"""
        sid = self._register(kind="model", entry="tileset.json",
                             bounds_wgs84=[100.0, 30.0, 101.0, 31.0])
        got = self.client.get("/api/services").json()["services"]
        desc = got[0]["overlay_desc"]
        self.assertEqual(desc["kind"], "raster_only_bbox")
        self.assertIn("tileset.json", got[0]["access_path"])

    def test_model_root_serves_entry_and_subpaths(self):
        """模型服务的入口与子资源都要能取到。

        回归：entry 曾被当成目录前缀拼在 sub 前，得到
        tileset.json/Data/r.b3dm 这种不存在的路径，子资源全 404，
        三维瓦片整片加载不出来。3D Tiles 的 tileset.json 内部用相对
        **自己所在目录**的 URI 引用资源，故 entry 不能参与拼接。
        """
        (self.data / "3dtiles" / "Data").mkdir(parents=True)
        (self.data / "3dtiles" / "tileset.json").write_bytes(b'{"asset":{}}')
        (self.data / "3dtiles" / "Data" / "r.b3dm").write_bytes(b"B3DM")
        sid = self._register(kind="model", entry="tileset.json",
                             root=str(self.data / "3dtiles"))

        # 根路由返回入口文件
        r = self.client.get(f"/api/svc/{sid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b'{"asset":{}}')

        # 子资源按 root 直接解析（不带 entry 前缀）
        r = self.client.get(f"/api/svc/{sid}/Data/r.b3dm")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"B3DM")
        self.assertEqual(r.headers.get("content-type"),
                         "application/octet-stream")

    def test_tile_service_root_gives_clear_error(self):
        """瓦片服务没有根级文件，根路由要给明确说明而不是含义不明的 404。"""
        sid = self._register()   # entry 为空
        r = self.client.get(f"/api/svc/{sid}")
        self.assertEqual(r.status_code, 404)
        self.assertIn("{z}/{x}/{y}", r.json()["detail"])

    def test_register_endpoint(self):
        r = self.client.post("/api/services", json={
            "name": "新服务", "kind": "imagery", "root": str(self.data),
            "grid": "mercator"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["name"], "新服务")
        self.assertFalse(r.json()["enabled"])   # 默认关闭

    def test_register_rejects_relative_path(self):
        r = self.client.post("/api/services", json={
            "kind": "imagery", "root": "output"})
        self.assertEqual(r.status_code, 400)

    def test_register_rejects_bad_kind(self):
        r = self.client.post("/api/services", json={
            "kind": "nonsense", "root": str(self.data)})
        self.assertEqual(r.status_code, 400)


if __name__ == "__main__":
    unittest.main()
