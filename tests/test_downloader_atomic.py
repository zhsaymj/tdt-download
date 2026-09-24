"""瓦片缓存写入必须是原子的:目标路径要么不存在,要么内容完整。

背景:缓存路径只由 provider.key + z + col + row 决定,与任务无关 ——
多 worker 下两个范围重叠的任务会写同一路径。若写不原子,另一进程会读到
写了一半的文件,而命中判定是 `exists() && size > 0`,残缺文件会被当成
有效瓦片进入拼接,且此后永远不会自愈。

测法说明(为什么不用"另起进程轮询中间态"):
200KB 的 write_bytes 在毫秒级完成,轮询进程来不及捕捉中间态,该测法在
未修复的实现上也常常直接通过,无法用于破坏性验证。改为直接验证原子写
**协议**本身:写入必须"先落同目录临时文件,再 os.replace 原子改名"。
用 spy 包住 os.replace,在改名发生的**那一刻**检查源文件状态 ——
断言源文件已存在、内容是完整 payload、且与目标路径不同名。
"""
from __future__ import annotations

import asyncio
import os
import unittest
from pathlib import Path
from unittest import mock

from backend.core.downloader import TileDownloader


class _FakeProvider:
    """provider 替身:隔离网络,只关心缓存写入行为。"""

    key = "tianditu_img"
    ext = "jpg"
    bands = 3

    def tile_url(self, col, row, z):
        return f"http://example.invalid/{z}/{col}/{row}"

    def is_empty_tile(self, data):
        return False


class _Resp:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    async def read(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _Session:
    def __init__(self, payload):
        self._payload = payload

    def get(self, url):
        return _Resp(self._payload)


class _Sem:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _make_downloader(cache: Path, **kw) -> TileDownloader:
    return TileDownloader(_FakeProvider(), cache_dir=cache, use_cache=True, **kw)


class TestAtomicCacheWrite(unittest.TestCase):
    def test_write_uses_temp_file_then_atomic_replace(self):
        """写缓存必须是"临时文件 + os.replace 原子改名"。

        在 os.replace 被调用的那一刻快照源文件状态:源文件必须已存在、
        大小等于完整 payload、且不能就是目标路径(否则等于原地改名,
        目标路径仍会暴露中间态)。
        """
        import tempfile

        payload = b"\xff\xd8\xff\xe0" + b"A" * 200000
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            dl = _make_downloader(cache)
            target = dl.tile_path(1, 2, 3)

            snapshots = []
            real_replace = os.replace

            def spy_replace(src, dst, *a, **kw):
                src, dst = Path(src), Path(dst)
                snapshots.append(
                    {
                        "src": src,
                        "dst": dst,
                        "src_exists": src.exists(),
                        "src_size": src.stat().st_size if src.exists() else None,
                        "src_bytes": src.read_bytes() if src.exists() else None,
                        "dst_existed_before": dst.exists(),
                    }
                )
                return real_replace(src, dst, *a, **kw)

            with mock.patch("os.replace", spy_replace):
                ok = asyncio.run(
                    dl._one(_Session(payload), _Sem(), 1, 2, 3)
                )

            self.assertTrue(ok, "下载应成功")
            self.assertTrue(
                snapshots,
                "未观察到 os.replace 调用 —— 缓存是直接写目标路径的非原子写,"
                "多 worker 下另一进程会读到残缺文件",
            )

            snap = snapshots[0]
            self.assertNotEqual(
                snap["src"], snap["dst"], "临时文件不能就是目标路径"
            )
            self.assertEqual(
                snap["src"].parent,
                snap["dst"].parent,
                "临时文件必须与目标同目录,否则 os.replace 可能跨卷、不再原子",
            )
            self.assertTrue(snap["src_exists"], "改名时临时文件应已存在")
            self.assertEqual(
                snap["src_size"],
                len(payload),
                "改名时临时文件必须是完整内容",
            )
            self.assertEqual(snap["src_bytes"], payload)
            self.assertFalse(
                snap["dst_existed_before"],
                "改名应发生在新文件首次落盘时,目标路径此前不该存在",
            )

            # 最终目标文件完整
            self.assertTrue(target.exists(), "瓦片应已写入")
            self.assertEqual(target.stat().st_size, len(payload))
            self.assertEqual(target.read_bytes(), payload)

            # 不应残留临时文件
            leftovers = [
                str(p) for p in cache.rglob("*") if p.is_file() and p != target
            ]
            self.assertEqual(leftovers, [], f"不应残留临时文件:{leftovers}")

    def test_replace_failure_is_retried_and_leaves_no_temp(self):
        """os.replace 失败应走重试,且不残留临时文件。

        Windows 上 os.replace 会被其它进程持有目标文件句柄挡住
        (PermissionError/WinError 32),属可重试的瞬时错误。
        """
        import tempfile

        payload = b"\xff\xd8\xff\xe0" + b"B" * 65536
        with tempfile.TemporaryDirectory() as td:
            cache = Path(td)
            dl = _make_downloader(cache, max_retries=2)
            target = dl.tile_path(4, 5, 6)

            real_replace = os.replace
            calls = {"n": 0}

            def flaky_replace(src, dst, *a, **kw):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise PermissionError(32, "文件被另一进程占用")
                return real_replace(src, dst, *a, **kw)

            with mock.patch("os.replace", flaky_replace):
                with mock.patch(
                    "asyncio.sleep", new=_no_sleep
                ):
                    ok = asyncio.run(
                        dl._one(_Session(payload), _Sem(), 4, 5, 6)
                    )

            self.assertTrue(ok, "首次改名失败后应重试成功,而非让任务崩掉")
            self.assertGreaterEqual(calls["n"], 2, "应至少重试一次")
            self.assertEqual(target.read_bytes(), payload, "重试后内容应完整")

            leftovers = [
                str(p) for p in cache.rglob("*") if p.is_file() and p != target
            ]
            self.assertEqual(leftovers, [], f"不应残留临时文件:{leftovers}")


async def _no_sleep(_seconds):
    return None


if __name__ == "__main__":
    unittest.main()
