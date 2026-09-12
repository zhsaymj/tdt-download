"""py3dtiles LAS/LAZ→3D Tiles 适配器测试。

不依赖真实 py3dtiles venv 与 LAS 样例:
- 命令构造直接断言参数列表(以 v12.1.1 sdist 源码核实的 CLI 为准);
- 可用性检查的成功/模块缺失/超时分支用 mock 拦截 subprocess.run
  (主 venv 未装 py3dtiles,无法真实试跑);
- 产物检测用临时目录伪造 tileset.json 与 points/*.pnts。
"""
import subprocess as sp
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from backend.core.processors.base import ProcessorError
from backend.core.processors.las_to_3dtiles import LasTo3dTiles


def _cfg(python: str):
    """构造只含 tools.py3dtiles_python 的最小配置桩。"""
    return SimpleNamespace(tools=SimpleNamespace(py3dtiles_python=python))


class BuildCmdTest(unittest.TestCase):
    """命令构造:`python -m py3dtiles convert <输入.las> --out <输出目录>`。

    参数拼写以 py3dtiles 12.1.1 sdist 源码(convert.py _init_parser)为准。
    """

    def test_build_cmd(self):
        p = LasTo3dTiles(python="tools/py3dtiles-venv/Scripts/python.exe")
        cmd = p.build_cmd(input_file="D:/las_sample/a.las", out_dir="out/3dtiles")
        self.assertEqual(cmd, ["tools/py3dtiles-venv/Scripts/python.exe",
                               "-m", "py3dtiles", "convert",
                               "D:/las_sample/a.las", "--out", "out/3dtiles"])

    def test_build_cmd_without_python_raises(self):
        p = LasTo3dTiles()
        with self.assertRaises(ProcessorError) as cm:
            p.build_cmd(input_file="a.las", out_dir="out")
        self.assertIn("py3dtiles_python", str(cm.exception))


class ParseProgressTest(unittest.TestCase):
    """进度解析:尽力解析 x/y 与 %;无法解析返回 None。

    py3dtiles 默认 verbose=0 无数字进度行,本解析器是保底能力,
    实际进度靠 progress_by_output_count 兜底。
    """

    def test_parse_ratio_line(self):
        p = LasTo3dTiles(python="x")
        self.assertAlmostEqual(p.parse_progress("converting 3/10"), 0.3)

    def test_parse_percent_line(self):
        p = LasTo3dTiles(python="x")
        self.assertAlmostEqual(p.parse_progress("process: 45%"), 0.45)

    def test_parse_noise_returns_none(self):
        p = LasTo3dTiles(python="x")
        self.assertIsNone(p.parse_progress("Writing 3dtiles"))
        self.assertIsNone(p.parse_progress(""))
        # 时间戳等含数字但非进度的内容
        self.assertIsNone(p.parse_progress("2026-09-12 10:00:00 start"))


class ExpectedOutputsTest(unittest.TestCase):
    """产物校验:tileset.json 存在且非空,且至少一个 .pnts。"""

    def test_expected_outputs_requires_tileset_and_pnts(self):
        p = LasTo3dTiles(python="x")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            # 空目录:无 pnts → 判失败
            with self.assertRaises(ProcessorError):
                p.expected_outputs(out)
            # 只有 tileset.json、没有任何 pnts → 仍判失败
            (out / "tileset.json").write_text("{}", encoding="utf-8")
            with self.assertRaises(ProcessorError) as cm:
                p.expected_outputs(out)
            self.assertIn("pnts", str(cm.exception))

    def test_expected_outputs_happy_path(self):
        p = LasTo3dTiles(python="x")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "tileset.json").write_text("{}", encoding="utf-8")
            # py3dtiles 实际布局:<out>/points/r0.pnts(PointTiler.name="points")
            pnts = out / "points" / "r0.pnts"
            pnts.parent.mkdir(parents=True)
            pnts.write_bytes(b"pnts")
            outputs = p.expected_outputs(out)
            self.assertEqual(outputs[0], out / "tileset.json")
            self.assertIn(pnts, outputs)


class CheckAvailableTest(unittest.TestCase):
    """可用性检查:解释器存在 + `python -m py3dtiles -h` 退出码 0。

    主 venv 未装 py3dtiles,成功/模块缺失/超时分支 mock subprocess.run;
    路径与启动失败分支用真实文件系统。
    """

    def test_available(self):
        p = LasTo3dTiles()
        with mock.patch(
                "backend.core.processors.las_to_3dtiles.subprocess.run",
                return_value=sp.CompletedProcess([], 0, stdout=b"", stderr=b"")):
            ok, _msg = p.check_available(_cfg(sys.executable))
        self.assertTrue(ok)

    def test_not_configured(self):
        ok, msg = LasTo3dTiles().check_available(_cfg(""))
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_missing_path(self):
        ok, msg = LasTo3dTiles().check_available(
            _cfg("不存在的路径/python_notexist_xyz.exe"))
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_module_not_installed(self):
        # 解释器存在但未装 py3dtiles:`-m py3dtiles` 退出码非 0
        p = LasTo3dTiles()
        with mock.patch(
                "backend.core.processors.las_to_3dtiles.subprocess.run",
                return_value=sp.CompletedProcess(
                    [], 1, stdout=b"", stderr=b"No module named py3dtiles")):
            ok, msg = p.check_available(_cfg(sys.executable))
        self.assertFalse(ok)
        self.assertIn("py3dtiles", msg)

    def test_probe_timeout(self):
        p = LasTo3dTiles()
        with mock.patch(
                "backend.core.processors.las_to_3dtiles.subprocess.run",
                side_effect=sp.TimeoutExpired(["python"], 15)):
            ok, msg = p.check_available(_cfg(sys.executable))
        self.assertFalse(ok)
        self.assertIn("超时", msg)

    def test_not_executable(self):
        # 存在的文件但不是可执行程序 → 试跑失败
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not an exe")
            path = f.name
        try:
            ok, _msg = LasTo3dTiles().check_available(_cfg(path))
            self.assertFalse(ok)
        finally:
            Path(path).unlink(missing_ok=True)


class FileCountProgressTest(unittest.TestCase):
    """文件计数兜底进度:解析不到文本进度时按 pnts 数量估算。"""

    def test_progress_by_output_count(self):
        with tempfile.TemporaryDirectory() as d:
            out = Path(d)
            (out / "points").mkdir()
            for i in range(2):
                (out / "points" / f"r{i}.pnts").write_bytes(b"x")
            self.assertAlmostEqual(
                LasTo3dTiles.progress_by_output_count(out, 4), 0.5)

    def test_progress_by_output_count_zero_total(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(LasTo3dTiles.progress_by_output_count(Path(d), 0))


if __name__ == "__main__":
    unittest.main()
