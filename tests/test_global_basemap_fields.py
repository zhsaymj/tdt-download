"""tasks 表新增两列:global_max_level / buffer_rings(设计 3.1)。"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend import db, models


class GlobalBasemapFieldsTest(unittest.TestCase):
    TMP: Path = None

    @classmethod
    def setUpClass(cls):
        cls.TMP = Path(tempfile.mkdtemp(prefix="tdt-gbf-"))

    def test_migration_registered(self):
        """_MIGRATIONS 里注册了两条 ALTER TABLE。"""
        self.assertIn("global_max_level", db._MIGRATIONS)
        self.assertIn("buffer_rings", db._MIGRATIONS)

    def test_create_task_persists(self):
        d = self.TMP / "p.db"
        with mock.patch.object(db, "DB_PATH", d):
            db.init_db()
            with mock.patch.object(models, "reserve_output_dir",
                                   return_value=self.TMP / "out"):
                task_id = models.create_task(
                    models.TaskCreate(
                        name="t", provider="tianditu_img",
                        bbox=[120.5, 30.5, 121.0, 30.9], levels=[18],
                        global_max_level=5, buffer_rings=2),
                    total=10)
            row = models.get_task(task_id)
        self.assertEqual(row["global_max_level"], 5)
        self.assertEqual(row["buffer_rings"], 2)

    def test_defaults_are_off_and_three_rings(self):
        """默认:global_max_level=0(不启用)、buffer_rings=3(实测 1 圈仍能看到边界)。"""
        d = self.TMP / "r.db"
        with mock.patch.object(db, "DB_PATH", d):
            db.init_db()
            with mock.patch.object(models, "reserve_output_dir",
                                   return_value=self.TMP / "out"):
                models.create_task(
                    models.TaskCreate(
                        name="t2", provider="tianditu_img",
                        bbox=[120.5, 30.5, 121.0, 30.9], levels=[18]),
                    total=10)
                raw = db.get_conn().execute(
                    "SELECT * FROM tasks WHERE name='t2'").fetchone()
                parsed = models._row_to_dict(raw)
        self.assertEqual(parsed["global_max_level"], 0)
        self.assertEqual(parsed["buffer_rings"], 3)


if __name__ == "__main__":
    unittest.main()