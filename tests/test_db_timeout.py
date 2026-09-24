"""验证 SQLite 连接启用了 busy_timeout(多进程并发写的关键)。"""
import sqlite3
import unittest
from unittest import mock


class TestDbTimeout(unittest.TestCase):
    def test_busy_timeout_enabled(self):
        from backend.db import get_conn
        conn = get_conn()
        try:
            got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(got, 5000,
                                "busy_timeout 需 >=5s,否则并发写直接报 locked")

    def test_wal_mode_enabled(self):
        from backend.db import get_conn
        conn = get_conn()
        try:
            got = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(got.lower(), "wal")

    def test_busy_timeout_set_explicitly_not_by_default(self):
        """busy_timeout 必须由 get_conn 显式设定,不能只吃 sqlite3.connect 的默认值。

        为什么需要这条:``sqlite3.connect()`` 的 ``timeout`` 参数默认为 5.0,
        它**顺带**把 busy_timeout 也设成 5000。所以 test_busy_timeout_enabled
        在没写 PRAGMA 时同样通过 —— 那条断言分辨不出「显式设定的」与「蹭默认值的」。
        而默认值不可依赖:一旦有人给 connect 传了更小的 timeout(如排查锁问题时
        顺手写上 timeout=0),并发写就会立刻报 "database is locked",行为测试
        却依旧全绿。这里人为把 connect 的 timeout 压到 0.05s,断言显式 PRAGMA
        仍把它拉回 >=5000。
        """
        import backend.db as db_module

        real_connect = sqlite3.connect

        def _connect_with_tiny_timeout(*args, **kwargs):
            kwargs["timeout"] = 0.05
            return real_connect(*args, **kwargs)

        # get_conn 里是 `sqlite3.connect(...)`,patch 模块属性即可生效;
        # 作用域仅限本 with 块,块内只调 get_conn,不会波及别的用例。
        with mock.patch.object(db_module.sqlite3, "connect",
                               _connect_with_tiny_timeout):
            conn = db_module.get_conn()
        try:
            got = conn.execute("PRAGMA busy_timeout").fetchone()[0]
        finally:
            conn.close()
        self.assertGreaterEqual(
            got, 5000,
            "busy_timeout 必须显式设置,不能依赖 sqlite3.connect 的 timeout 默认值")


if __name__ == "__main__":
    unittest.main()
