"""fanvanzh/3dtiles OSGB→3D Tiles 适配器测试。

全部用临时目录构造假目录树 + ``sys.executable`` 模拟可执行文件,
不依赖真实 3dtiles.exe 与 OSGB 样例数据。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.core.processors.base import ProcessorError
from backend.core.processors.osgb_to_3dtiles import OsgbTo3dTiles


def _cfg(exe: str):
    """构造只含 tools.tiles3d_exe 的最小配置桩。"""
    return SimpleNamespace(tools=SimpleNamespace(tiles3d_exe=exe))


def _make_osgb_tree(root: Path, with_metadata: bool = True) -> None:
    """构造符合 smart3d 组织方式的假 OSGB 目录树。"""
    tile = root / "Data" / "Tile_000_000"
    tile.mkdir(parents=True)
    (tile / "Tile_000_000.osgb").write_bytes(b"fake-osgb")
    if with_metadata:
        (root / "metadata.xml").write_text("<ModelMetadata/>", encoding="utf-8")


class BuildCmdTest(unittest.TestCase):
    """命令构造:-f osgb -i <输入目录> -o <输出目录>(以 fanvanzh README 为准)。"""

    def test_build_cmd(self):
        p = OsgbTo3dTiles(exe="tools/3dtiles/3dtiles.exe")
        cmd = p.build_cmd(input_dir="D:/osgb_sample", out_dir="out/3dtiles")
        self.assertTrue(cmd[0].endswith("3dtiles.exe"))
        self.assertIn("-f", cmd)
        self.assertIn("osgb", cmd)
        self.assertIn("-i", cmd)
        self.assertIn("D:/osgb_sample", cmd)
        self.assertIn("-o", cmd)
        self.assertIn("out/3dtiles", cmd)
        # 默认走顶层 LOD 金字塔(上游默认开启),不带 --no-pyramid
        self.assertNotIn("--no-pyramid", cmd)

    def test_build_cmd_no_pyramid_flag(self):
        """no_pyramid=True 追加 --no-pyramid,回退为平铺各 Tile 子树。"""
        p = OsgbTo3dTiles(exe="tools/3dtiles/3dtiles.exe")
        cmd = p.build_cmd(input_dir="D:/osgb_sample", out_dir="out/3dtiles",
                          no_pyramid=True)
        self.assertEqual(cmd[-1], "--no-pyramid")
        self.assertIn("-f", cmd)
        self.assertIn("osgb", cmd)

    def test_build_cmd_normalizes_exe_separators(self):
        """配置里的正斜杠路径要转成本机分隔符。

        Windows 上 CreateProcess 不认 "tools/3dtiles/3dtile.exe":
        Path.exists 为真但 subprocess 抛 WinError 2(系统找不到指定的文件)。
        """
        p = OsgbTo3dTiles(exe="tools/3dtiles/3dtile.exe")
        cmd = p.build_cmd(input_dir="D:/in", out_dir="out")
        self.assertEqual(cmd[0], os.path.normpath("tools/3dtiles/3dtile.exe"))
        self.assertNotIn("/", cmd[0])


class ParseProgressTest(unittest.TestCase):
    """进度解析:返回 0~1;无法解析返回 None。"""

    def test_parse_ratio_line(self):
        p = OsgbTo3dTiles(exe="x.exe")
        self.assertAlmostEqual(p.parse_progress("converting 3/10"), 0.3)
        # env_logger 风格前缀的同一行
        self.assertAlmostEqual(
            p.parse_progress("INFO: 2026-09-12 10:00:00 - converting 1/4"), 0.25)

    def test_parse_percent_line(self):
        p = OsgbTo3dTiles(exe="x.exe")
        self.assertAlmostEqual(p.parse_progress("process: 45%"), 0.45)

    def test_parse_noise_returns_none(self):
        p = OsgbTo3dTiles(exe="x.exe")
        self.assertIsNone(p.parse_progress("INFO: task over"))
        self.assertIsNone(p.parse_progress(""))
        # 时间戳等含数字但非进度的内容
        self.assertIsNone(p.parse_progress("INFO: 2026-09-12 10:00:00 - start"))


class ExpectedOutputsTest(unittest.TestCase):
    """产物校验:tileset.json 存在且非空,且至少一个 .b3dm。"""

    def test_expected_outputs_requires_tileset_and_b3dm(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            # 空目录:无 b3dm → 判失败
            with self.assertRaises(ProcessorError):
                p.expected_outputs(out)
            # 只有 tileset.json、没有任何 b3dm → 仍判失败
            (out / "tileset.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ProcessorError) as cm:
                p.expected_outputs(out)
            self.assertIn("b3dm", str(cm.exception))

    def test_expected_outputs_happy_path(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "tileset.json").write_text("{}", encoding="utf-8")
            b3dm = out / "Data" / "Tile_000_000" / "Tile_000_000.b3dm"
            b3dm.parent.mkdir(parents=True)
            b3dm.write_bytes(b"b3dm")
            outputs = p.expected_outputs(out)
            self.assertEqual(outputs[0], out / "tileset.json")
            self.assertIn(b3dm, outputs)


class PreflightTest(unittest.TestCase):
    """输入预检:目录存在、含 .osgb;metadata.xml 缺失仅警告不阻断。"""

    def test_preflight_ok(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with tempfile.TemporaryDirectory() as d:
            _make_osgb_tree(Path(d))
            self.assertEqual(p.preflight(d), [])

    def test_preflight_warns_without_metadata(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with tempfile.TemporaryDirectory() as d:
            _make_osgb_tree(Path(d), with_metadata=False)
            warnings = p.preflight(d)
            self.assertEqual(len(warnings), 1)
            self.assertIn("metadata.xml", warnings[0])

    def test_preflight_missing_dir_raises(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with self.assertRaises(ProcessorError):
            p.preflight("不存在的目录_xyz")

    def test_preflight_no_osgb_raises(self):
        p = OsgbTo3dTiles(exe="x.exe")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ProcessorError) as cm:
                p.preflight(d)
            self.assertIn("osgb", str(cm.exception))


class CheckAvailableTest(unittest.TestCase):
    """可用性检查:路径存在 + `-h` 可跑。用 sys.executable 模拟。"""

    def test_available_with_runnable_exe(self):
        p = OsgbTo3dTiles()
        ok, _msg = p.check_available(_cfg(sys.executable))
        self.assertTrue(ok)

    def test_not_configured(self):
        p = OsgbTo3dTiles()
        ok, msg = p.check_available(_cfg(""))
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_missing_path(self):
        p = OsgbTo3dTiles()
        ok, msg = p.check_available(_cfg("不存在的路径/3dtiles.exe"))
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_not_executable(self):
        # 存在的文件但不是可执行程序 → 试跑失败
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not an exe")
            path = f.name
        try:
            ok, _msg = OsgbTo3dTiles().check_available(_cfg(path))
            self.assertFalse(ok)
        finally:
            Path(path).unlink(missing_ok=True)


class FileCountProgressTest(unittest.TestCase):
    """文件计数兜底进度:解析不到文本进度时按 b3dm 数量估算。"""

    def test_progress_by_output_count(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            for i in range(2):
                (out / f"{i}.b3dm").write_bytes(b"x")
            self.assertAlmostEqual(
                OsgbTo3dTiles.progress_by_output_count(out, 4), 0.5)

    def test_progress_by_output_count_zero_total(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(OsgbTo3dTiles.progress_by_output_count(Path(d), 0))


if __name__ == "__main__":
    unittest.main()
