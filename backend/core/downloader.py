"""并发瓦片下载器:信号量限速 + 指数退避重试 + 断点续传。

瓦片按 {cache_dir}/{provider.key}/{z}/{col}_{row}.{ext} 缓存;
已存在且非空的文件直接跳过,实现断点续传。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

import aiohttp

from ..providers.base import TileProvider
from .tiling import TileRange

# 进度回调:progress(downloaded_delta, failed_delta)
ProgressCb = Callable[[int, int], None]


class TileDownloader:
    def __init__(
        self,
        provider: TileProvider,
        cache_dir: Path,
        concurrency: int = 8,
        max_retries: int = 3,
        timeout: int = 30,
        use_cache: bool = True,
    ):
        self.provider = provider
        self.cache_dir = cache_dir
        self.concurrency = concurrency
        self.max_retries = max_retries
        self.timeout = timeout
        self.use_cache = use_cache

    def tile_path(self, col: int, row: int, z: int) -> Path:
        return (
            self.cache_dir
            / self.provider.key
            / str(z)
            / f"{col}_{row}.{self.provider.ext}"
        )

    async def download_range(
        self,
        tr: TileRange,
        progress: ProgressCb | None = None,
        should_stop: Callable[[], bool] | None = None,
    ) -> tuple[int, int, bool]:
        """下载一个瓦片区间。

        should_stop() 返回 True 时尽快停止(暂停/取消)。
        返回 (成功数, 失败数, stopped)。
        """
        sem = asyncio.Semaphore(self.concurrency)
        # 默认头(天地图对 Referer/UA 有校验);provider 可覆盖(如 Esri 需模拟客户端头)
        headers = {
            "User-Agent": "Mozilla/5.0 (TiandituDownloader)",
            "Referer": "https://www.tianditu.gov.cn/",
        }
        provider_headers = getattr(self.provider, "headers", None)
        if provider_headers:
            headers = {**headers, **provider_headers}
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        ok = fail = 0
        stopped = False

        async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
            tasks = [
                asyncio.create_task(self._one(session, sem, col, row, tr.z))
                for col, row in tr.iter_tiles()
            ]
            for coro in asyncio.as_completed(tasks):
                success = await coro
                if success:
                    ok += 1
                    if progress:
                        progress(1, 0)
                else:
                    fail += 1
                    if progress:
                        progress(0, 1)
                # 检查停止请求:取消尚未完成的下载协程后跳出
                if should_stop and should_stop():
                    stopped = True
                    for tk in tasks:
                        if not tk.done():
                            tk.cancel()
                    break
        return ok, fail, stopped

    async def _one(self, session, sem, col, row, z) -> bool:
        path = self.tile_path(col, row, z)
        # 使用缓存时:已下载则跳过(断点续传);不使用缓存时:强制重新下载覆盖
        if self.use_cache and path.exists() and path.stat().st_size > 0:
            return True

        path.parent.mkdir(parents=True, exist_ok=True)
        url = self.provider.tile_url(col, row, z)

        for attempt in range(self.max_retries + 1):
            try:
                async with sem:
                    async with session.get(url) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            if data:
                                # 占位"空瓦片"(如 Esri 超出可用 LOD 返回的 67 字节
                                # 空 LERC)不写缓存:写了会被断点续传当成有效数据,
                                # 拼接时又解不出像素,最终产出全 nodata 的成果。
                                # 请求本身是成功的(该处确实无数据),故不计失败。
                                if self.provider.is_empty_tile(data):
                                    return True
                                path.write_bytes(data)
                                return True
                        # 非 200 或空响应,进入重试
            except asyncio.CancelledError:
                raise  # 暂停/取消时被取消,直接向上抛出
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

            if attempt < self.max_retries:
                await asyncio.sleep(min(2 ** attempt, 8))  # 指数退避,封顶 8s

        return False
