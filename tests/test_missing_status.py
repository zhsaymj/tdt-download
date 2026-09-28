"""下载器的代理传递与 404 语义分流。

两件事:
  1. provider.proxy 必须传给 ClientSession(session 级,不是逐请求)
  2. missing_statuses 命中时:不重试、不计失败、不写缓存

为什么 404 不能重试:Google 对无影像位置返回 404(海洋/极地/无覆盖)。
一个纯海域的 0.05 度选区约 1800 张瓦片,按 max_retries=3 会白跑 5400 次请求。
"""
import asyncio
import tempfile
import unittest
from pathlib import Path

from backend.core.downloader import TileDownloader
from backend.core.tiling import TileRange
from backend.providers.base import TileProvider


class _FakeResponse:
    def __init__(self, status: int, body: bytes):
        self.status = status
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """记录每个 URL 被请求了几次,并按预设返回状态码。"""

    def __init__(self, status: int, body: bytes = b"xx"):
        self.status = status
        self.body = body
        self.calls: list[str] = []

    def get(self, url, **kwargs):
        self.calls.append(url)
        return _FakeResponse(self.status, self.body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Missing404Provider(TileProvider):
    """模拟 Google:404 表示该瓦片无影像。"""
    key = "fake_google"
    ext = "jpg"
    bands = 3

    def tile_url(self, col, row, z):
        return f"https://example.com/{z}/{col}/{row}.jpg"

    def missing_statuses(self) -> frozenset[int]:
        return frozenset({404})

    @property
    def proxy(self) -> str | None:
        return "http://127.0.0.1:6789"


class _PlainProvider(TileProvider):
    """不声明 missing_statuses:404 按普通失败处理。"""
    key = "fake_plain"
    ext = "jpg"
    bands = 3

    def tile_url(self, col, row, z):
        return f"https://example.com/{z}/{col}/{row}.jpg"


def _run(coro):
    """跑一个协程并关闭事件循环(不关会报 ResourceWarning 噪音)。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestMissingStatus(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self._tmp.name)
        self.tr = TileRange(10, 0, 0, 0, 0)     # 单张瓦片

    def tearDown(self):
        self._tmp.cleanup()

    def _download(self, provider, session, retries=3):
        """跑一次 download_range,返回 (ok, fail, stopped, session 构造参数)。

        asyncio.sleep 被打桩:重试的指数退避是 min(2**attempt, 8),不桩掉的话
        test_404_without_declaration_is_failure(首次 + 2 次重试)实测要跑 14 秒,
        整个文件 45 秒 —— 对单元测试不可接受。要验的是"请求了几次",不是退避时长。
        """
        dl = TileDownloader(provider, cache_dir=self.cache,
                            concurrency=2, max_retries=retries, timeout=5)
        import backend.core.downloader as mod
        orig = mod.aiohttp.ClientSession
        captured = {}

        def fake_client_session(**kwargs):
            captured.update(kwargs)
            return session

        async def _no_sleep(*_a, **_kw):
            return None

        mod.aiohttp.ClientSession = fake_client_session
        orig_sleep = asyncio.sleep
        asyncio.sleep = _no_sleep
        try:
            ok, fail, stopped = _run(dl.download_range(self.tr))
        finally:
            mod.aiohttp.ClientSession = orig
            asyncio.sleep = orig_sleep
        return ok, fail, stopped, captured

    def test_404_counts_as_success_not_failure(self):
        """该处确实无数据,请求本身是成功的,不该计入 failed。"""
        session = _FakeSession(404)
        ok, fail, _stopped, _kw = self._download(_Missing404Provider(), session)
        self.assertEqual(ok, 1)
        self.assertEqual(fail, 0)

    def test_404_not_retried(self):
        """关键:只请求 1 次,而非 max_retries+1 次。"""
        session = _FakeSession(404)
        self._download(_Missing404Provider(), session, retries=3)
        self.assertEqual(len(session.calls), 1)

    def test_404_not_cached(self):
        """无数据不该落盘,否则断点续传会把它当有效缓存。"""
        session = _FakeSession(404)
        self._download(_Missing404Provider(), session)
        self.assertEqual(list(self.cache.rglob("*.jpg")), [])

    def test_404_without_declaration_is_failure(self):
        """未声明 missing_statuses 的数据源:404 仍按失败处理并重试。"""
        session = _FakeSession(404)
        ok, fail, _stopped, _kw = self._download(_PlainProvider(), session,
                                                 retries=2)
        self.assertEqual(ok, 0)
        self.assertEqual(fail, 1)
        self.assertEqual(len(session.calls), 3)     # 首次 + 2 次重试

    def test_proxy_passed_to_session(self):
        """代理必须是 session 级:_one 里有重试循环,逐请求传容易漏。"""
        session = _FakeSession(404)
        _ok, _fail, _stopped, kwargs = self._download(_Missing404Provider(),
                                                     session)
        self.assertEqual(kwargs.get("proxy"), "http://127.0.0.1:6789")

    def test_no_proxy_when_provider_has_none(self):
        session = _FakeSession(404)
        _ok, _fail, _stopped, kwargs = self._download(_PlainProvider(), session)
        self.assertIsNone(kwargs.get("proxy"))

    def test_trust_env_not_enabled(self):
        """不能开 trust_env:会把天地图也绕进用户的系统代理(静默回归)。"""
        session = _FakeSession(404)
        _ok, _fail, _stopped, kwargs = self._download(_PlainProvider(), session)
        self.assertNotIn("trust_env", kwargs)

    def test_200_still_cached(self):
        """正常瓦片行为不变。"""
        session = _FakeSession(200, b"realbytes")
        ok, fail, _stopped, _kw = self._download(_Missing404Provider(), session)
        self.assertEqual((ok, fail), (1, 0))
        self.assertEqual(len(list(self.cache.rglob("*.jpg"))), 1)


if __name__ == "__main__":
    unittest.main()
