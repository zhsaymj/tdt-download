"""瓦片 PNG 写入的两条加固(需求39 的后续)。

**现象**:OSM 切到 99.3% 时 8 个线程**同时**报
`libpng: No IDATs written into file`,整个阶段失败。

**根因**(实测):
* 失败张数 ≈ 线程池大小(8),且是连续一列里最靠前的几张 —— 不是某张瓦片的数据问题
* 同一张瓦片单独写两次**都成功** → 不是路径/权限/机制问题
* 内存 11.4G 可用、磁盘 20G 可用 → 不是资源枯竭
* `rasterio.Env.__enter__` 用 `threading.local()` 判断"是不是最外层",于是**每个
  线程都以为自己最外层**,各自 `defenv()` 设置、退出时还原**进程级全局**的 GDAL
  配置 → 多线程每张瓦片都改全局配置,是真实的数据竞争
* GDAL 的 PNG 驱动在"带 alpha 波段"这条路上本就是已知脆弱路径

加固:① 全局 PAM 配置只设一次,不在每张瓦片里进 `rasterio.Env`;
② PNG 写入失败重试一次 —— 一张瓦片写失败不该让整个阶段(已切一万多张)前功尽弃。
"""
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import numpy as np
from rasterio.transform import from_bounds

from backend.core import gdal_env  # noqa: F401  (import 即生效)
from backend.core.osm import _write_png


def _arr() -> np.ndarray:
    a = np.zeros((4, 256, 256), dtype=np.uint8)
    a[:3] = 90
    a[3, 8:248, 8:248] = 255
    return a


class PngWriteTest(unittest.TestCase):
    def test_writes_valid_png(self):
        with TemporaryDirectory() as d:
            p = Path(d) / "t.png"
            _write_png(p, _arr(), from_bounds(0, 0, 1, 1, 256, 256))
            self.assertTrue(p.exists() and p.stat().st_size > 0)

    def test_retries_once_on_transient_failure(self):
        """★ 核心 ★ 一次瞬时失败要被重试吸收,而不是让整个阶段失败。

        真因(全局配置竞争)已修,但那条路在 GDAL 侧本就脆弱;一张瓦片写失败就
        丢掉已切好的一万多张、整个阶段报错,代价太不成比例。
        """
        import rasterio.shutil as rs
        calls = {"n": 0}
        real = rs.copy

        def flaky(src, dst, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("libpng: No IDATs written into file")
            return real(src, dst, **kw)

        with TemporaryDirectory() as d:
            p = Path(d) / "t.png"
            with mock.patch.object(rs, "copy", side_effect=flaky):
                _write_png(p, _arr(), from_bounds(0, 0, 1, 1, 256, 256))
            self.assertEqual(calls["n"], 2, "第一次失败后应重试")
            self.assertTrue(p.exists() and p.stat().st_size > 0,
                            "重试后应写出真实 PNG")

    def test_still_raises_when_retry_also_fails(self):
        """重试也失败时要抛出 —— 不能静默产出坏瓦片。"""
        import rasterio.shutil as rs
        with TemporaryDirectory() as d:
            p = Path(d) / "t.png"
            with mock.patch.object(rs, "copy",
                                   side_effect=RuntimeError("No IDATs")):
                with self.assertRaises(RuntimeError):
                    _write_png(p, _arr(), from_bounds(0, 0, 1, 1, 256, 256))

    def test_concurrent_writes_all_succeed(self):
        """多线程并发写也要全成功(改前每张瓦片都改全局 GDAL 配置)。"""
        with TemporaryDirectory() as d:
            root = Path(d)
            errs = []

            def work(i):
                try:
                    _write_png(root / f"t{i}.png", _arr(),
                               from_bounds(0, 0, 1, 1, 256, 256))
                except Exception as ex:            # noqa: BLE001
                    errs.append(ex)

            ts = [threading.Thread(target=work, args=(i,)) for i in range(120)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(errs, [], f"并发写入出现失败:{errs[:2]}")
            for i in range(120):
                self.assertTrue((root / f"t{i}.png").stat().st_size > 0)


class NoPerTileGlobalEnvTest(unittest.TestCase):
    """切瓦片的写入函数不得在每张瓦片里进 `rasterio.Env`(进程级全局竞争)。"""

    def _check(self, name: str):
        from pathlib import Path as _P
        raw = (_P(__file__).resolve().parent.parent
               / "backend" / "core" / name).read_text(encoding="utf-8")
        # 剥注释再断言 —— 代码里正**说明**"为什么不用 rasterio.Env",那是正常文字。
        # (本项目为此栽过:断言 includes('rasterio.Env') 会命中自己的注释。)
        src = "\n".join(l for l in raw.splitlines()
                        if not l.lstrip().startswith("#"))
        self.assertNotIn("with rasterio.Env", src,
                         f"{name} 仍在每张瓦片里进 rasterio.Env —— 多线程切瓦片时"
                         f"它会把进程级全局 GDAL 配置改来改去(见模块说明)")
        self.assertIn("gdal_env", raw,
                      f"{name} 应 import core.gdal_env,让 PAM 配置只设一次")

    def test_osm_writer(self):
        self._check("osm.py")

    def test_tms_writer(self):
        self._check("tms.py")


class GdalEnvTest(unittest.TestCase):
    def test_pam_disabled_process_wide(self):
        import os
        self.assertEqual(os.environ.get("GDAL_PAM_ENABLED"), "NO")


if __name__ == "__main__":
    unittest.main()
