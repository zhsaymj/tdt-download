import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from backend.core.service_registry import (
    add_service, check_path, list_services, remove_service, update_service,
)


class PathSafetyTest(unittest.TestCase):
    """路径穿越是这类文件服务最经典的漏洞，逐种形态都要拦。"""

    def test_normal_relative_path(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            (root / "a").mkdir(parents=True)
            (root / "a" / "b.png").write_bytes(b"x")
            got = check_path(root, "a/b.png")
            self.assertEqual(got, (root / "a" / "b.png").resolve())

    def test_dotdot_escape_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            with self.assertRaises(PermissionError):
                check_path(root, "../secret.txt")

    def test_nested_dotdot_escape_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            with self.assertRaises(PermissionError):
                check_path(root, "a/../../secret.txt")

    def test_absolute_path_rejected(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            with self.assertRaises(PermissionError):
                check_path(root, "/etc/passwd")

    def test_empty_path_is_root(self):
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            self.assertEqual(check_path(root, ""), root)

    def test_backslash_is_not_a_separator_on_posix_only(self):
        """反斜杠在 POSIX 上是合法文件名字符，不应被当作穿越。"""
        with TemporaryDirectory() as d:
            root = Path(d).resolve()
            # 不应抛异常（在 Windows 上会被解析为分隔符，但仍在 root 内）
            check_path(root, "a\\b.png")


class RegistryCrudTest(unittest.TestCase):
    def setUp(self):
        from backend import db
        # sqlite 连接帧引用导致 db 文件被锁，清理失败不应掩盖真实断言结果
        # （与 tests/test_models_3d.py 的既有做法一致）
        self._tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self._old = db.DB_PATH
        db.DB_PATH = Path(self._tmp.name) / "test.db"
        self.addCleanup(self._restore)
        db.init_db()

    def _restore(self):
        from backend import db
        db.DB_PATH = self._old

    def _mk(self, **kw):
        d = kw.pop("dir", None)
        base = {"name": "测试", "kind": "imagery", "root": str(d),
                "entry": "", "grid": "mercator", "flip_y": False,
                "minzoom": 0, "maxzoom": 18, "bounds_wgs84": None,
                "bounds_approx": False, "source": "manual"}
        base.update(kw)
        return add_service(**base)

    def test_add_and_list(self):
        with TemporaryDirectory() as d:
            self._mk(dir=d)
            got = list_services()
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["name"], "测试")

    def test_new_service_disabled_by_default(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            got = list_services()
            self.assertEqual(got[0]["enabled"], 0)
            self.assertTrue(sid)

    def test_update_enabled(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            update_service(sid, enabled=1)
            self.assertEqual(list_services()[0]["enabled"], 1)

    def test_update_name(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            update_service(sid, name="改过了")
            self.assertEqual(list_services()[0]["name"], "改过了")

    def test_remove(self):
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            remove_service(sid)
            self.assertEqual(list_services(), [])

    def test_bounds_roundtrip(self):
        with TemporaryDirectory() as d:
            self._mk(dir=d, bounds_wgs84=[100.0, 30.0, 101.0, 31.0])
            got = list_services()[0]
            self.assertEqual(got["bounds_wgs84"], [100.0, 30.0, 101.0, 31.0])

    def test_health_flags_missing_root(self):
        from backend.core.service_registry import service_health
        with TemporaryDirectory() as d:
            sid = self._mk(dir=d)
            self.assertTrue(service_health(sid)["ok"])
        # 目录已随 TemporaryDirectory 删除
        self.assertFalse(service_health(sid)["ok"])


if __name__ == "__main__":
    unittest.main()
