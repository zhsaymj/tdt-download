"""PDAL LAS/LAZ→DEM/DSM 适配器测试。

不依赖真实 pdal.exe 与 LAS 样例:
- pipeline JSON 生成直接断言结构(readers.las / filters / writers.gdal);
- preflight 解析用构造的 `pdal info --summary` JSON 文本;
- 产物校验用 rasterio 在临时目录写真实 GeoTIFF;
- run_pipeline 用 mock 拦截 run,验证命令构造与临时文件生命周期。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import numpy as np
import rasterio

from backend.core.processors.base import ProcResult, ProcessorError
from backend.core.processors.las_to_dem import LasToDem


def _cfg(exe: str):
    """构造只含 tools.pdal_exe 的最小配置桩。"""
    return SimpleNamespace(tools=SimpleNamespace(pdal_exe=exe))


def _write_tif(path: Path) -> None:
    """用 rasterio 写一个真实可打开的 GeoTIFF(10x10 float32)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.arange(100, dtype="float32").reshape(10, 10)
    with rasterio.open(path, "w", driver="GTiff", width=10, height=10,
                       count=1, dtype="float32", nodata=-9999) as dst:
        dst.write(data, 1)


# 构造的 `pdal info --summary` 输出(字段结构以 PDAL 2.x 实际输出为准)
_INFO_JSON = json.dumps({
    "filename": "a.las",
    "pdal_version": "2.7.0 (git-version: Release)",
    "summary": {
        "bbox": {"maxx": 638982.55, "maxy": 853535.43, "maxz": 586.38,
                 "minx": 635619.85, "miny": 848899.7, "minz": 406.59},
        "count": 10650136,
        "srs": {"compoundwkt": "PROJCS[...]", "horizontal": "PROJCS[...]",
                "isgeographic": False, "isgeocentric": False,
                "prettywkt": "PROJCS[CGCS2000 / 3-degree Gauss-Kruger zone 39]",
                "proj4": "+proj=tmerc +lat_0=0 +lon_0=117 +k=1 +x_0=39500000",
                "vertical": ""},
    },
})


class BuildPipelineTest(unittest.TestCase):
    """pipeline JSON 生成:DSM/DEM 两种结构 + CRS 三种策略。"""

    def test_build_pipeline_dsm(self):
        p = LasToDem(pdal_exe="pdal")
        pipe = p.build_pipeline(input="a.las", output="out/a_dsm.tif",
                                kind="dsm", resolution=1.0, crs="EPSG:4547")
        d = json.loads(pipe)
        self.assertEqual(d["pipeline"][-1]["type"], "writers.gdal")

    def test_dsm_stages(self):
        p = LasToDem(pdal_exe="pdal")
        d = json.loads(p.build_pipeline(input="a.las", output="out/a_dsm.tif",
                                        kind="dsm", resolution=1.0))
        stages = d["pipeline"]
        # DSM:readers.las → writers.gdal(不做地面分类)
        self.assertEqual([s["type"] for s in stages],
                         ["readers.las", "writers.gdal"])
        self.assertEqual(stages[0]["filename"], "a.las")
        writer = stages[-1]
        self.assertEqual(writer["filename"], "out/a_dsm.tif")
        self.assertEqual(writer["output_type"], "max")  # DSM 取最高点
        self.assertEqual(writer["resolution"], 1.0)
        self.assertIn("nodata", writer)

    def test_dem_stages(self):
        p = LasToDem(pdal_exe="pdal")
        d = json.loads(p.build_pipeline(input="a.las", output="out/a_dem.tif",
                                        kind="dem", resolution=0.5))
        stages = d["pipeline"]
        # DEM:smrf 地面分类 → range 只留地面点 → idw 插值
        self.assertEqual([s["type"] for s in stages],
                         ["readers.las", "filters.smrf", "filters.range",
                          "writers.gdal"])
        self.assertEqual(stages[2]["limits"], "Classification[2:2]")
        self.assertEqual(stages[-1]["output_type"], "idw")
        self.assertEqual(stages[-1]["resolution"], 0.5)

    def test_crs_override_writes_reader_override_srs(self):
        p = LasToDem(pdal_exe="pdal")
        d = json.loads(p.build_pipeline(input="a.las", output="o.tif",
                                        kind="dsm", resolution=1.0,
                                        crs="EPSG:4547"))
        self.assertEqual(d["pipeline"][0]["override_srs"], "EPSG:4547")
        self.assertEqual(p.last_warnings, [])

    def test_crs_empty_keeps_las_header_srs(self):
        # pc_crs="" → 沿用 LAS 头 SRS,不写 override_srs
        p = LasToDem(pdal_exe="pdal")
        d = json.loads(p.build_pipeline(input="a.las", output="o.tif",
                                        kind="dsm", resolution=1.0, crs=""))
        self.assertNotIn("override_srs", d["pipeline"][0])
        self.assertEqual(p.last_warnings, [])

    def test_crs_local_no_srs_with_warning(self):
        p = LasToDem(pdal_exe="pdal")
        d = json.loads(p.build_pipeline(input="a.las", output="o.tif",
                                        kind="dsm", resolution=1.0,
                                        crs="local"))
        self.assertNotIn("override_srs", d["pipeline"][0])
        self.assertEqual(len(p.last_warnings), 1)
        self.assertIn("local", p.last_warnings[0])

    def test_invalid_kind_raises(self):
        p = LasToDem(pdal_exe="pdal")
        with self.assertRaises(ProcessorError):
            p.build_pipeline(input="a.las", output="o.tif",
                             kind="xyz", resolution=1.0)

    def test_non_positive_resolution_raises(self):
        # pc_resolution=0(自动估算)由上层完成,本层只接受正数
        p = LasToDem(pdal_exe="pdal")
        with self.assertRaises(ProcessorError):
            p.build_pipeline(input="a.las", output="o.tif",
                             kind="dsm", resolution=0)


class BuildCmdTest(unittest.TestCase):
    """命令构造:`pdal pipeline <临时json>`;空 exe 守卫。"""

    def test_build_cmd(self):
        p = LasToDem(pdal_exe="tools/pdal/bin/pdal.exe")
        cmd = p.build_cmd(pipeline_file="tmp/pipe.json")
        self.assertTrue(cmd[0].endswith("pdal.exe"))
        self.assertEqual(cmd[1:], ["pipeline", "tmp/pipe.json"])

    def test_build_cmd_without_exe_raises(self):
        p = LasToDem()
        with self.assertRaises(ProcessorError) as cm:
            p.build_cmd(pipeline_file="x.json")
        self.assertIn("pdal_exe", str(cm.exception))


class RunPipelineTest(unittest.TestCase):
    """run_pipeline:写临时 pipeline JSON → 调 run → 清理临时文件。"""

    def test_run_pipeline_writes_temp_json_and_cleans_up(self):
        p = LasToDem(pdal_exe="pdal")
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            f = Path(cmd[2])
            seen["existed_during_run"] = f.exists()
            seen["json"] = json.loads(f.read_text(encoding="utf-8"))
            return ProcResult(ok=True)

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "a_dsm.tif"
            with mock.patch.object(p, "run", side_effect=fake_run):
                p.run_pipeline(input="a.las", output=out, kind="dsm",
                               resolution=1.0, crs="EPSG:4547")
            self.assertEqual(seen["cmd"][:2], ["pdal", "pipeline"])
            self.assertTrue(seen["existed_during_run"])
            self.assertEqual(seen["json"]["pipeline"][-1]["filename"], str(out))
            self.assertEqual(seen["json"]["pipeline"][0]["override_srs"],
                             "EPSG:4547")
            # run 返回后临时 pipeline 文件已清理
            self.assertFalse(Path(seen["cmd"][2]).exists())

    def test_run_pipeline_cleans_up_on_cancel(self):
        """run 抛 ProcessorCancelled(取消)时 finally 仍清理临时文件。"""
        from backend.core.processors.base import ProcessorCancelled
        p = LasToDem(pdal_exe="pdal")
        seen = {}

        def cancel_run(cmd, **kw):
            seen["tmp"] = Path(cmd[2])
            raise ProcessorCancelled()

        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(p, "run", side_effect=cancel_run):
                with self.assertRaises(ProcessorCancelled):
                    p.run_pipeline(input="a.las", output=Path(d) / "a_dsm.tif",
                                   kind="dsm", resolution=1.0)
            self.assertFalse(seen["tmp"].exists())


class PreflightSubprocessTest(unittest.TestCase):
    """preflight 实际执行 pdal info 的失败分支(mock subprocess.run)。"""

    def _las(self, d: str) -> Path:
        f = Path(d) / "a.las"
        f.write_bytes(b"LASF")  # 只需存在,pdal 调用被 mock
        return f

    def test_preflight_timeout(self):
        import subprocess as sp
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("backend.core.processors.las_to_dem.subprocess.run",
                            side_effect=sp.TimeoutExpired(["pdal"], 30)):
                with self.assertRaises(ProcessorError) as ctx:
                    p.preflight(self._las(d))
            self.assertIn("超时", ctx.exception.args[0])

    def test_preflight_nonzero_exit(self):
        import subprocess as sp
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch(
                    "backend.core.processors.las_to_dem.subprocess.run",
                    return_value=sp.CompletedProcess(
                        ["pdal"], 1, stdout=b"", stderr=b"bad file")):
                with self.assertRaises(ProcessorError) as ctx:
                    p.preflight(self._las(d))
            self.assertIn("损坏", ctx.exception.args[0])

    def test_preflight_oserror(self):
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            with mock.patch("backend.core.processors.las_to_dem.subprocess.run",
                            side_effect=OSError("WinError 2")):
                with self.assertRaises(ProcessorError) as ctx:
                    p.preflight(self._las(d))
            self.assertIn("无法启动", ctx.exception.args[0])


class ParseInfoSummaryTest(unittest.TestCase):
    """`pdal info --summary` 输出解析:点数/bbox/SRS。"""

    def test_parse_full_summary(self):
        info = LasToDem.parse_info_summary(_INFO_JSON)
        self.assertEqual(info["points"], 10650136)
        self.assertAlmostEqual(info["bbox"]["minx"], 635619.85)
        self.assertAlmostEqual(info["bbox"]["maxy"], 853535.43)
        self.assertIn("+proj=tmerc", info["srs"])

    def test_parse_missing_srs(self):
        text = json.dumps({"summary": {
            "bbox": {"minx": 0, "miny": 0, "maxx": 1, "maxy": 1},
            "count": 42}})
        info = LasToDem.parse_info_summary(text)
        self.assertEqual(info["points"], 42)
        self.assertEqual(info["srs"], "")

    def test_parse_invalid_json_raises(self):
        with self.assertRaises(ProcessorError):
            LasToDem.parse_info_summary("这不是 JSON")


class ExpectedOutputsTest(unittest.TestCase):
    """产物校验:tif 存在非空 + 可被 rasterio 打开。"""

    def test_ok_with_real_tif(self):
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x_dsm.tif"
            p.build_pipeline(input="a.las", output=out, kind="dsm",
                             resolution=1.0)
            _write_tif(out)
            self.assertEqual(p.expected_outputs(Path(d)), [out])

    def test_corrupt_tif_raises(self):
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x_dem.tif"
            p.build_pipeline(input="a.las", output=out, kind="dem",
                             resolution=1.0)
            out.write_bytes(b"this is not a tiff at all")
            with self.assertRaises(ProcessorError):
                p.expected_outputs(Path(d))

    def test_missing_file_left_to_base_check(self):
        # 文件缺失/空文件不在本层抛错,交给 base.run 的"未生成预期产物"判定
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "x_dsm.tif"
            p.build_pipeline(input="a.las", output=out, kind="dsm",
                             resolution=1.0)
            self.assertEqual(p.expected_outputs(Path(d)), [out])

    def test_called_before_build_pipeline_raises(self):
        p = LasToDem(pdal_exe="pdal")
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaises(ProcessorError):
                p.expected_outputs(Path(d))


class CheckAvailableTest(unittest.TestCase):
    """可用性检查:路径存在(或 PATH 可寻)+ `--version` 可跑。"""

    def test_available_with_runnable_exe(self):
        # python --version 退出码 0,可模拟 pdal --version
        p = LasToDem()
        ok, _msg = p.check_available(_cfg(sys.executable))
        self.assertTrue(ok)

    def test_not_configured(self):
        ok, msg = LasToDem().check_available(_cfg(""))
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_missing_path(self):
        ok, msg = LasToDem().check_available(
            _cfg("不存在的路径/pdal_notexist_xyz.exe"))
        self.assertFalse(ok)
        self.assertIn("不存在", msg)

    def test_not_executable(self):
        # 存在的文件但不是可执行程序 → 试跑失败
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"not an exe")
            path = f.name
        try:
            ok, _msg = LasToDem().check_available(_cfg(path))
            self.assertFalse(ok)
        finally:
            Path(path).unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
