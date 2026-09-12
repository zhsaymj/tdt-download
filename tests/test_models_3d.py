"""三维任务(本地 osgb/点云)的模型字段、落库读回与旧库迁移测试。"""
import asyncio
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import db as db_module
from backend.config import settings


class _TempDbCase(unittest.TestCase):
    """把 DB 与输出目录指到临时位置,避免测试污染真实数据。"""

    def setUp(self):
        # ignore_cleanup_errors:Windows 上失败用例的 traceback 会暂时持有
        # sqlite 连接帧引用导致 db 文件被锁,清理失败不应掩盖真实断言结果
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self._old_db_path = db_module.DB_PATH
        self._old_output_dir = settings.output.dir
        db_module.DB_PATH = root / "app.db"
        settings.output.dir = str(root / "output")
        self.addCleanup(self._restore)
        db_module.init_db()

    def _restore(self):
        db_module.DB_PATH = self._old_db_path
        settings.output.dir = self._old_output_dir


class PointCloudFieldModelTest(_TempDbCase):
    """TaskCreate 接受 pc_crs/pc_resolution,create_task 落库后可读回。"""

    def test_task_create_pc_fields_default(self):
        from backend.models import TaskCreate

        data = TaskCreate(provider="local_pointcloud", bbox=[0, 0, 0, 0])
        self.assertEqual(data.pc_crs, "")
        self.assertEqual(data.pc_resolution, 0.0)

    def test_task_create_accepts_pc_fields(self):
        from backend.models import TaskCreate

        data = TaskCreate(provider="local_pointcloud", bbox=[0, 0, 0, 0],
                          pc_crs="EPSG:4547", pc_resolution=0.5)
        self.assertEqual(data.pc_crs, "EPSG:4547")
        self.assertEqual(data.pc_resolution, 0.5)

    def test_create_task_roundtrips_pc_fields(self):
        from backend.models import TaskCreate, create_task, get_task

        data = TaskCreate(name="点云任务", provider="local_pointcloud",
                          bbox=[0, 0, 0, 0], export="dsm,dem,tile_3d",
                          source_path="G:/data/las",
                          pc_crs="EPSG:4547", pc_resolution=0.5)
        task_id = create_task(data, 0, 0)
        task = get_task(task_id)
        self.assertEqual(task["pc_crs"], "EPSG:4547")
        self.assertEqual(task["pc_resolution"], 0.5)
        # 本地源无下载阶段,点云阶段按 order 排序
        self.assertEqual([s["key"] for s in task["stages"]],
                         ["pc_dsm", "pc_dem", "pc_tile_3d"])

    def test_create_task_pc_fields_roundtrip_default(self):
        from backend.models import TaskCreate, create_task, get_task

        data = TaskCreate(name="点云默认", provider="local_pointcloud",
                          bbox=[0, 0, 0, 0], export="dsm",
                          source_path="G:/data/a.las")
        task = get_task(create_task(data, 0, 0))
        self.assertEqual(task["pc_crs"], "")
        self.assertEqual(task["pc_resolution"], 0.0)

    def test_update_task_can_rewrite_pc_fields(self):
        from backend.models import TaskCreate, create_task, get_task, update_task

        data = TaskCreate(name="点云改参", provider="local_pointcloud",
                          bbox=[0, 0, 0, 0], export="dsm",
                          source_path="G:/data/a.las",
                          pc_crs="EPSG:4547", pc_resolution=0.5)
        task_id = create_task(data, 0, 0)
        update_task(task_id, pc_crs="local", pc_resolution=1.0)
        task = get_task(task_id)
        self.assertEqual(task["pc_crs"], "local")
        self.assertEqual(task["pc_resolution"], 1.0)

    def test_osgb_task_builds_convert_stage_without_levels(self):
        # local_osgb 没有瓦片级别概念:不选级别、不做瓦片预估也能建任务
        from backend.models import TaskCreate, create_task, get_task

        data = TaskCreate(name="osgb任务", provider="local_osgb",
                          bbox=[0, 0, 0, 0], export="tile_3d",
                          source_path="G:/data/osgb")
        task = get_task(create_task(data, 0, 0))
        self.assertEqual(task["provider"], "local_osgb")
        self.assertEqual([s["key"] for s in task["stages"]], ["convert_3d"])


class Local3DTaskCreateApiTest(_TempDbCase):
    """_create_local_task 对 local_osgb/local_pointcloud 的源校验(跳过栅格 inspect)。"""

    def _create(self, data):
        from backend.api.tasks import _create_local_task
        from backend.core.queue import task_queue

        with mock.patch.object(task_queue, "enqueue",
                               new=mock.AsyncMock()) as enq:
            resp = asyncio.run(_create_local_task(data))
        enq.assert_awaited_once_with(resp["id"])
        return resp

    def test_osgb_accepts_directory(self):
        from backend.models import TaskCreate, get_task

        with tempfile.TemporaryDirectory() as d:
            osgb = Path(d) / "osgb模型"
            osgb.mkdir()
            data = TaskCreate(name="osgb任务", provider="local_osgb",
                              bbox=[0, 0, 0, 0], export="tile_3d",
                              source_path=str(osgb))
            resp = self._create(data)
            task = get_task(resp["id"])
        self.assertEqual(resp["status"], "pending")
        self.assertEqual(resp["total"], 0)
        self.assertEqual(task["provider"], "local_osgb")
        self.assertEqual([s["key"] for s in task["stages"]], ["convert_3d"])

    def test_osgb_rejects_file(self):
        # OSGB 是目录结构(Data/ + metadata.xml),给文件要拒绝
        from fastapi import HTTPException
        from backend.models import TaskCreate

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "metadata.xml"
            f.write_text("<xml/>", encoding="utf-8")
            data = TaskCreate(provider="local_osgb", bbox=[0, 0, 0, 0],
                              export="tile_3d", source_path=str(f))
            with self.assertRaises(HTTPException) as cm:
                self._create(data)
        self.assertEqual(cm.exception.status_code, 400)

    def test_pointcloud_accepts_single_las_file(self):
        from backend.models import TaskCreate, get_task

        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "tile_001.las"
            las.write_bytes(b"LASF")
            data = TaskCreate(name="点云任务", provider="local_pointcloud",
                              bbox=[0, 0, 0, 0], export="dsm",
                              source_path=str(las),
                              pc_crs="EPSG:4547", pc_resolution=0.5)
            resp = self._create(data)
            task = get_task(resp["id"])
        self.assertEqual(resp["status"], "pending")
        self.assertEqual([s["key"] for s in task["stages"]], ["pc_dsm"])
        self.assertEqual(task["pc_crs"], "EPSG:4547")
        self.assertEqual(task["pc_resolution"], 0.5)

    def test_pointcloud_accepts_directory_with_laz(self):
        # 目录时枚举 las/laz(扩展名大小写不敏感),非空即可
        from backend.models import TaskCreate

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "tiles" ).mkdir()
            (Path(d) / "tiles" / "a.LAZ").write_bytes(b"LASF")
            data = TaskCreate(provider="local_pointcloud", bbox=[0, 0, 0, 0],
                              export="dem", source_path=str(d))
            resp = self._create(data)
        self.assertEqual(resp["status"], "pending")

    def test_pointcloud_rejects_directory_without_las(self):
        from fastapi import HTTPException
        from backend.models import TaskCreate

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "readme.txt").write_text("x", encoding="utf-8")
            data = TaskCreate(provider="local_pointcloud", bbox=[0, 0, 0, 0],
                              export="dsm", source_path=str(d))
            with self.assertRaises(HTTPException) as cm:
                self._create(data)
        self.assertEqual(cm.exception.status_code, 400)

    def test_pointcloud_rejects_non_las_file(self):
        from fastapi import HTTPException
        from backend.models import TaskCreate

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.txt"
            f.write_text("x", encoding="utf-8")
            data = TaskCreate(provider="local_pointcloud", bbox=[0, 0, 0, 0],
                              export="dsm", source_path=str(f))
            with self.assertRaises(HTTPException) as cm:
                self._create(data)
        self.assertEqual(cm.exception.status_code, 400)

    def test_pointcloud_pc_crs_valid_forms_accepted(self):
        # pc_crs 合法形式:空(自动读 LAS 头)、local(大小写不敏感)、EPSG:数字
        from backend.models import TaskCreate

        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "a.las"
            las.write_bytes(b"LASF")
            for crs in ("", "local", "LOCAL", "EPSG:4547", "epsg:4547"):
                with self.subTest(pc_crs=crs):
                    data = TaskCreate(provider="local_pointcloud",
                                      bbox=[0, 0, 0, 0], export="dsm",
                                      source_path=str(las), pc_crs=crs)
                    resp = self._create(data)
                    self.assertEqual(resp["status"], "pending")

    def test_pointcloud_pc_crs_bad_form_rejected(self):
        from fastapi import HTTPException
        from backend.models import TaskCreate

        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "a.las"
            las.write_bytes(b"LASF")
            for crs in ("4547", "EPSG:", "EPSG:abc", "wgs84", "EPSG:4547 "):
                with self.subTest(pc_crs=crs):
                    data = TaskCreate(provider="local_pointcloud",
                                      bbox=[0, 0, 0, 0], export="dsm",
                                      source_path=str(las), pc_crs=crs)
                    with self.assertRaises(HTTPException) as cm:
                        self._create(data)
                    self.assertEqual(cm.exception.status_code, 400)
                    self.assertIn("pc_crs", cm.exception.detail)


class OldDbMigrationTest(unittest.TestCase):
    """旧库(无 pc_crs/pc_resolution 列)经 init_db 迁移后补齐新列。"""

    def test_migrate_adds_pc_columns(self):
        old_schema = """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            provider TEXT NOT NULL,
            bbox TEXT NOT NULL,
            z_min INTEGER NOT NULL,
            z_max INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        """
        # ignore_cleanup_errors:init_db 的 sqlite 连接(WAL 模式)在 Windows 上
        # 由 GC 决定释放时机,临时 db 可能仍被占用,清理失败不应掩盖迁移断言结果
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as d:
            db_path = Path(d) / "old.db"
            conn = sqlite3.connect(db_path)
            conn.execute(old_schema)
            conn.execute(
                "INSERT INTO tasks (id, name, provider, bbox, z_min, z_max,"
                " created_at, updated_at) VALUES"
                " ('t1', '旧任务', 'tianditu_img', '[0,0,1,1]', 1, 2,"
                "  '2026-01-01', '2026-01-01')")
            conn.commit()
            conn.close()

            old = db_module.DB_PATH
            db_module.DB_PATH = db_path
            try:
                db_module.init_db()
            finally:
                db_module.DB_PATH = old

            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            try:
                cols = {r["name"] for r in
                        conn.execute("PRAGMA table_info(tasks)")}
                row = conn.execute(
                    "SELECT pc_crs, pc_resolution FROM tasks WHERE id='t1'"
                ).fetchone()
            finally:
                conn.close()

        self.assertIn("pc_crs", cols)
        self.assertIn("pc_resolution", cols)
        # 迁移后存量行回填默认值:空=自动读 LAS 头,0=分辨率自适应
        self.assertEqual(row["pc_crs"], "")
        self.assertEqual(row["pc_resolution"], 0)


if __name__ == "__main__":
    unittest.main()
