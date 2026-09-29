"""提交前的代理预检。

要点:
  - 预检在**主进程**做,不留给 worker
  - 判不了(超时)时**放行**,不能把网络抖动当成"代理不通"而拒绝提交
  - 错误文案要含"改完之后怎么办"(配置项名 + 需重启)
"""
import asyncio
import unittest

from backend.api.tasks import _precheck_network_provider


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestPrecheck(unittest.TestCase):
    def _patch_probe(self, result):
        """result: True=通, False=不通, None=判不了。"""
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda provider, timeout=6: result
        return orig, mod

    def test_reachable_passes(self):
        orig, mod = self._patch_probe(True)
        try:
            ok, msg = _run(_precheck_network_provider("google_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_unreachable_rejected_with_actionable_message(self):
        orig, mod = self._patch_probe(False)
        try:
            ok, msg = _run(_precheck_network_provider("google_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertFalse(ok)
        # 文案必须告诉用户改哪里、以及改完要重启
        self.assertIn("google.proxy", msg)
        self.assertIn("重启", msg)

    def test_esri_message_points_to_its_own_config(self):
        orig, mod = self._patch_probe(False)
        try:
            ok, msg = _run(_precheck_network_provider("esri_imagery"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertFalse(ok)
        self.assertIn("esri_imagery.proxy", msg)

    def test_undetermined_passes(self):
        """判不了要放行 —— 与 probe_max_level 返回 None 时的取舍一致。
        把网络抖动当成"代理不通"会让用户在能下的时候也提交不了。"""
        orig, mod = self._patch_probe(None)
        try:
            ok, _msg = _run(_precheck_network_provider("google_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)

    def test_tianditu_skipped(self):
        """天地图直连可用,不该被预检拦(也不该白跑一次网络请求)。"""
        called = []
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda p, timeout=6: called.append(p)
        try:
            ok, _msg = _run(_precheck_network_provider("tianditu_img"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(called, [])

    def test_dem_skipped(self):
        """Esri Terrain3D 直连可用,同样跳过。"""
        called = []
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda p, timeout=6: called.append(p)
        try:
            ok, _msg = _run(_precheck_network_provider("esri_terrain"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(called, [])

    def test_local_source_skipped(self):
        called = []
        import backend.api.tasks as mod
        orig = mod._probe_provider_reachable
        mod._probe_provider_reachable = lambda p, timeout=6: called.append(p)
        try:
            ok, _msg = _run(_precheck_network_provider("local_image"))
        finally:
            mod._probe_provider_reachable = orig
        self.assertTrue(ok)
        self.assertEqual(called, [])


class TestConnectionRefusedDetection(unittest.TestCase):
    """★ 回归护栏 ★ "代理没开"必须被识别为确证失败,不能当成"判不了"放行。

    实现期实测发现的坑:代理端口拒绝连接时 urllib 抛 URLError(内层
    ConnectionRefusedError)。若一律按"连不上 -> 判不了 -> 放行"处理,
    本预检就永远拦不住任何东西 —— 而"代理没开"恰恰是它存在的理由。
    """

    def test_direct_connection_refused(self):
        from backend.api.tasks import _is_connection_refused
        self.assertTrue(_is_connection_refused(ConnectionRefusedError("x")))

    def test_wrapped_in_urlerror_reason(self):
        """urllib 把底层异常包在 URLError.reason 里。"""
        import urllib.error
        from backend.api.tasks import _is_connection_refused
        e = urllib.error.URLError(ConnectionRefusedError("[WinError 10061] 拒绝"))
        self.assertTrue(_is_connection_refused(e))

    def test_wrapped_via_cause_chain(self):
        from backend.api.tasks import _is_connection_refused
        inner = ConnectionRefusedError("refused")
        outer = OSError("outer")
        outer.__cause__ = inner
        self.assertTrue(_is_connection_refused(outer))

    def test_timeout_is_not_refused(self):
        """超时不是确证失败 —— 必须能区分开,否则网络抖动会误拒提交。"""
        from backend.api.tasks import _is_connection_refused
        self.assertFalse(_is_connection_refused(TimeoutError("timed out")))
        import urllib.error
        self.assertFalse(_is_connection_refused(
            urllib.error.URLError(TimeoutError("timed out"))))

    def test_http_error_is_not_refused(self):
        import urllib.error
        from backend.api.tasks import _is_connection_refused
        self.assertFalse(_is_connection_refused(urllib.error.HTTPError(
            "u", 404, "nf", {}, None)))

    def test_cyclic_chain_terminates(self):
        """异常链理论上可能有环,不能死循环。"""
        from backend.api.tasks import _is_connection_refused
        a = OSError("a")
        b = OSError("b")
        a.__cause__ = b
        b.__cause__ = a
        self.assertFalse(_is_connection_refused(a))


class TestProxyConfigKey(unittest.TestCase):
    def test_keys_per_provider(self):
        from backend.api.tasks import _proxy_config_key
        self.assertEqual(_proxy_config_key("google_img"), "google.proxy")
        self.assertEqual(_proxy_config_key("esri_imagery"),
                         "esri_imagery.proxy")
        self.assertEqual(_proxy_config_key("tianditu_img"), "")

    def test_needs_proxy_predicate(self):
        from backend.api.tasks import _needs_proxy_provider
        for k in ("google_img", "google_hybrid", "google_road",
                  "google_terrain", "esri_imagery"):
            self.assertTrue(_needs_proxy_provider(k), k)
        for k in ("tianditu_img", "esri_terrain", "local_image", ""):
            self.assertFalse(_needs_proxy_provider(k), k)


if __name__ == "__main__":
    unittest.main()
