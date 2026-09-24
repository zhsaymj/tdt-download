"""runner_3d 独立管线与 queue 分发的单元测试。

三个外部处理器（OsgbTo3dTiles / LasToDem / LasTo3dTiles）一律 mock，
patch 点为 backend.core.runner_3d 模块内的类引用；产物文件由 mock 的
side_effect 伪造。watcher 线程默认关闭（_ENABLE_WATCHER=False），暂停/取消
通过 ProcessorCancelled + request_pause 确定性模拟，避免线程竞态；
仅 test_watcher_* 用例保留 watcher，验证「队列控制标志 → cancel_event」
这条生产暂停/取消的唯一通路。
"""

import asyncio
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import rasterio
from rasterio.transform import from_origin

from backend import db as db_module
from backend.config import settings
from backend.core.processors.base import ProcessorCancelled
from backend.models import TaskCreate, create_task, get_task

BBOX = [116.0, 39.0, 117.0, 40.0]

#: 测试内的控制状态镜像(替代旧 task_queue._control)。
#:
#: 改造后控制标志在 **worker 子进程**里(worker.py 的 control_state),主进程的
#: task_queue 已不再持有它 —— 旧 TaskQueue._control / control_of 随之删除。
#: 本文件的用例都在主进程里直接跑 runner,故自带一份 dict 扮演 worker 的角色:
#: 写入处相当于 worker 收到 pause/cancel 信号,should_stop 相当于 worker 交给
#: runner 的那个读闭包。
_control: dict[str, str] = {}


class _TempDbCase(unittest.TestCase):
    """与 test_models_3d 同款的临时库/临时输出目录基建。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self._old_db = db_module.DB_PATH
        self._old_outdir = settings.output.dir
        db_module.DB_PATH = tmp / "test.db"
        settings.output.dir = str(tmp / "output")
        db_module.init_db()
        self.addCleanup(self._restore)

    def _restore(self):
        db_module.DB_PATH = self._old_db
        settings.output.dir = self._old_outdir


def _mk_src(root: Path, name="data.las") -> Path:
    src = root / name
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b"xxxx")
    return src


class Runner3dCase(_TempDbCase):
    def setUp(self):
        super().setUp()
        self.tmp = Path(self._tmp.name)

    def _patch_processors(self, enable_watcher: bool = False):
        p_osgb = mock.patch("backend.core.runner_3d.OsgbTo3dTiles").start()
        p_dem = mock.patch("backend.core.runner_3d.LasToDem").start()
        p_py = mock.patch("backend.core.runner_3d.LasTo3dTiles").start()
        if not enable_watcher:
            mock.patch(
                "backend.core.runner_3d._ENABLE_WATCHER", False
            ).start()
        self.addCleanup(mock.patch.stopall)
        for proc in (p_osgb.return_value, p_dem.return_value, p_py.return_value):
            proc.check_available.return_value = (True, "ok")
        p_osgb.return_value.preflight.return_value = []
        p_osgb.return_value.parse_progress.return_value = None
        p_osgb.progress_by_output_count.return_value = None
        p_py.return_value.parse_progress.return_value = None
        p_dem.return_value.preflight.return_value = {
            "points": 100,
            "bbox": {"minx": 0.0, "miny": 0.0, "maxx": 10.0, "maxy": 10.0},
            "srs": "",
            "warnings": [],
        }
        p_dem.return_value.last_warnings = []
        p_py.return_value.last_warnings = []
        return p_osgb, p_dem, p_py

    def _run(self, task_id: str):
        from backend.core.runner_3d import run_task

        # runner 要求注入 should_stop 闭包(Task 4)。本用例的控制通路是模块级
        # _control(测试里写它就相当于 worker 收到 pause/cancel 信号),故按
        # worker 侧的方式包一个读闭包传进去。返回值带 pause/cancel 原因,
        # runner 才能把暂停与取消落成不同状态。
        asyncio.run(run_task(
            task_id, lambda m: None,
            lambda: _control.get(task_id)))

    @staticmethod
    def _ok_result(*outputs: Path):
        return mock.Mock(ok=True, outputs=[str(p) for p in outputs], message="")

    @staticmethod
    def _write_tif(path: Path, origin_x: float):
        """写一幅 2x2 float32 tif，x 范围 [origin_x, origin_x+2)。"""
        profile = {
            "driver": "GTiff",
            "height": 2,
            "width": 2,
            "count": 1,
            "dtype": "float32",
            "crs": "EPSG:4326",
            "transform": from_origin(origin_x, 2, 1, 1),
            "nodata": -9999.0,
        }
        with rasterio.open(path, "w", **profile) as ds:
            ds.write(np.zeros((1, 2, 2), dtype=np.float32))

    # 1. local_osgb 任务只跑 convert_3d 阶段
    def test_osgb_task_runs_only_convert_3d(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        osgb_dir = self.tmp / "osgb"
        for i in range(3):
            d = osgb_dir / "Data" / f"Tile_{i}"
            d.mkdir(parents=True, exist_ok=True)
            (d / f"Tile_{i}.osgb").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="三维OSGB",
                provider="local_osgb",
                bbox=BBOX,
                source_path=str(osgb_dir),
                export="tile_3d",
            ),
            total=3,
        )

        def fake_run(cmd, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            (out / "a.b3dm").write_bytes(b"x")
            return self._ok_result(out / "tileset.json")

        p_osgb.return_value.run.side_effect = fake_run
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "done", msg=str(final.get("stages")))
        self.assertEqual(p_osgb.return_value.run.call_count, 1)
        p_dem.return_value.run_pipeline.assert_not_called()
        p_py.return_value.run.assert_not_called()
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(set(stages), {"convert_3d"})
        self.assertEqual(stages["convert_3d"]["status"], "done")
        # estimated_total 取输入目录递归 .osgb 总数
        self.assertEqual(stages["convert_3d"]["total"], 3)

    # 2. local_pointcloud 按 export 集合跑 pc_dsm/pc_dem/pc_tile_3d
    def test_pointcloud_runs_stages_by_export_set(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="点云全量",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="dsm,dem,tile_3d",
                pc_resolution=1.0,
            ),
            total=1,
        )
        kinds = []

        def fake_pipeline(**kwargs):
            kinds.append(kwargs["kind"])
            out = Path(kwargs["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"tif")
            return self._ok_result(out)

        def fake_py_run(cmd, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            (out / "a.pnts").write_bytes(b"x")
            return self._ok_result(out / "tileset.json")

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        p_py.return_value.run.side_effect = fake_py_run
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "done", msg=str(final.get("stages")))
        # order：pc_dsm(10) 先于 pc_dem(20)
        self.assertEqual(kinds, ["dsm", "dem"])
        self.assertEqual(p_py.return_value.run.call_count, 1)
        stages = {s["key"]: s["status"] for s in final["stages"]}
        self.assertEqual(
            stages, {"pc_dsm": "done", "pc_dem": "done", "pc_tile_3d": "done"}
        )

    # 3. export="dem" 只跑 pc_dem
    def test_pointcloud_export_dem_only(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="只要DEM",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="dem",
                pc_resolution=1.0,
            ),
            total=1,
        )

        def fake_pipeline(**kwargs):
            out = Path(kwargs["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"tif")
            return self._ok_result(out)

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "done", msg=str(final.get("stages")))
        self.assertEqual(p_dem.return_value.run_pipeline.call_count, 1)
        self.assertEqual(
            p_dem.return_value.run_pipeline.call_args.kwargs["kind"], "dem"
        )
        p_py.return_value.run.assert_not_called()
        stages = {s["key"]: s["status"] for s in final["stages"]}
        self.assertEqual(stages, {"pc_dem": "done"})

    # 4. ProcessorCancelled（协作式停止）→ 任务落 paused
    def test_stopped_task_lands_paused(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="暂停任务",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="dsm",
                pc_resolution=1.0,
            ),
            total=1,
        )

        def fake_pipeline(**kwargs):
            _control[task_id] = "pause"
            raise ProcessorCancelled("pdal")

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        try:
            self._run(task_id)
        finally:
            _control.pop(task_id, None)

        self.assertEqual(get_task(task_id)["status"], "paused")

    # 5. 某阶段产物缺失 → 该阶段 fail、任务 failed，但独立阶段仍继续跑完
    def test_missing_output_fails_stage_but_independent_stages_continue(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="缺产物",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="dsm,dem,tile_3d",
                pc_resolution=1.0,
            ),
            total=1,
        )

        def fake_pipeline(**kwargs):
            # dsm 故意不产物 → 阶段失败；dem 正常
            if kwargs["kind"] == "dem":
                out = Path(kwargs["output"])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"tif")
                return self._ok_result(out)
            return mock.Mock(ok=True, outputs=[], message="")

        def fake_py_run(cmd, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        p_py.return_value.run.side_effect = fake_py_run
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "failed")
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(stages["pc_dsm"]["status"], "failed")
        self.assertIn("产物缺失", stages["pc_dsm"]["message"])
        self.assertEqual(stages["pc_dem"]["status"], "done")
        self.assertEqual(stages["pc_tile_3d"]["status"], "done")

    # 6. convert_3d：fanvanzh 部分瓦片失败（failed:/ERROR 日志）→ 阶段失败
    def test_convert_3d_detects_partial_tile_failure_lines(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        osgb_dir = self.tmp / "osgb"
        (osgb_dir / "Tile_005").mkdir(parents=True)
        (osgb_dir / "Tile_005" / "Tile_005.osgb").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="部分失败",
                provider="local_osgb",
                bbox=BBOX,
                source_path=str(osgb_dir),
                export="tile_3d",
            ),
            total=1,
        )

        def fake_run(cmd, **kwargs):
            # fanvanzh 单 Tile 失败仅打日志仍 exit 0，由钩子扫描发现
            kwargs["on_stdout_line"]("failed: Tile_005.osgb")
            kwargs["on_stderr_line"]("some error occurred")
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_osgb.return_value.run.side_effect = fake_run
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "failed")
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(stages["convert_3d"]["status"], "failed")
        self.assertIn("2 个节点转换失败", stages["convert_3d"]["message"])
        self.assertIn("failed: Tile_005.osgb", stages["convert_3d"]["message"])

    # 6b. convert_3d：失败节点占比低于阈值 → 阶段仍 done，只在消息里留 warning
    #     （源数据个别 osgb 缺页属常见情形，不该让整个成果无法预览）
    def test_convert_3d_tolerates_few_failures(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        osgb_dir = self.tmp / "osgb"
        # 200 个节点做分母，单行错误占 0.5% < 1% 阈值
        for i in range(200):
            d = osgb_dir / f"Tile_{i:03d}"
            d.mkdir(parents=True)
            (d / f"Tile_{i:03d}.osgb").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="少量失败",
                provider="local_osgb",
                bbox=BBOX,
                source_path=str(osgb_dir),
                export="tile_3d",
            ),
            total=1,
        )

        def fake_run(cmd, **kwargs):
            kwargs["on_stderr_line"](
                "Error reading file Tile_066_L25_00031100.osgb: file not found")
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_osgb.return_value.run.side_effect = fake_run
        self._run(task_id)

        final = get_task(task_id)
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(stages["convert_3d"]["status"], "done")
        self.assertIn("成果已生成", stages["convert_3d"]["message"])
        self.assertIn("1 个节点转换失败", stages["convert_3d"]["message"])

    # 6c. convert_3d：上游 LOD 金字塔静默回退成平铺时，阶段仍 done，
    #     但要在消息里说明「根节点精细度下降」，否则成果能打开但显示粗糙，
    #     用户无从判断是数据问题还是转换问题。
    def test_convert_3d_flags_pyramid_fallback(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        osgb_dir = self.tmp / "osgb"
        (osgb_dir / "Tile_005").mkdir(parents=True)
        (osgb_dir / "Tile_005" / "Tile_005.osgb").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="金字塔回退",
                provider="local_osgb",
                bbox=BBOX,
                source_path=str(osgb_dir),
                export="tile_3d",
            ),
            total=1,
        )

        def fake_run(cmd, **kwargs):
            # 真实日志带 spdlog 前缀，形如
            # `[2026-09-21 14:49:41.123] [warning] pyramid: cannot parse
            #  grid from Tile_+065_+052, fallback`；不含 failed/error 关键词，
            # 故只影响金字塔提示，不触发阶段失败判定。
            kwargs["on_stderr_line"](
                "[2026-09-21 14:49:41.123] [warning] pyramid: "
                "cannot parse grid from Tile_+065_+052, fallback")
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_osgb.return_value.run.side_effect = fake_run
        self._run(task_id)

        final = get_task(task_id)
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(stages["convert_3d"]["status"], "done")
        self.assertIn("LOD 金字塔回退为平铺", stages["convert_3d"]["message"])
        self.assertIn("cannot parse grid", stages["convert_3d"]["message"])

    # 6d. convert_3d：金字塔正常生成时不得出现回退提示（避免误报）
    def test_convert_3d_does_not_flag_pyramid_when_ok(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        osgb_dir = self.tmp / "osgb"
        (osgb_dir / "Tile_005").mkdir(parents=True)
        (osgb_dir / "Tile_005" / "Tile_005.osgb").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="金字塔正常",
                provider="local_osgb",
                bbox=BBOX,
                source_path=str(osgb_dir),
                export="tile_3d",
            ),
            total=1,
        )

        def fake_run(cmd, **kwargs):
            kwargs["on_stdout_line"](
                "[2026-09-21 14:49:41.123] [info] pyramid node r: "
                "tiles=384 geoms=420 simplified=420 kept=0 tex=8")
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_osgb.return_value.run.side_effect = fake_run
        self._run(task_id)

        final = get_task(task_id)
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(stages["convert_3d"]["status"], "done")
        self.assertNotIn("回退", stages["convert_3d"].get("message") or "")

    def _run_tile3d_with_crs(self, crs: str):
        """跑一个 export=tile_3d 的单文件点云任务，返回 LasTo3dTiles 类 mock。"""
        p_osgb, p_dem, p_py = self._patch_processors()
        src = _mk_src(self.tmp, name=f"data_{crs or 'empty'}.las")
        task_id = create_task(
            TaskCreate(
                name=f"切片_{crs or '空'}",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="tile_3d",
                pc_crs=crs,
            ),
            total=1,
        )

        def fake_py_run(cmd, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_py.return_value.run.side_effect = fake_py_run
        self._run(task_id)
        return p_py

    # 7. pc_crs 为 EPSG 码 → build_cmd 收到 --srs_in <crs> + --srs_out 4978
    def test_pc_tile_3d_passes_srs_when_pc_crs_is_epsg(self):
        p_py = self._run_tile3d_with_crs("EPSG:4547")
        kwargs = p_py.return_value.build_cmd.call_args.kwargs
        self.assertEqual(kwargs["srs_in"], "EPSG:4547")
        self.assertEqual(kwargs["srs_out"], "4978")

    # 8. pc_crs 为 local / 空 → 不传 srs 参数（runner 传空串，由 build_cmd 省略）
    def test_pc_tile_3d_omits_srs_for_local_crs(self):
        p_py = self._run_tile3d_with_crs("local")
        kwargs = p_py.return_value.build_cmd.call_args.kwargs
        self.assertEqual(kwargs["srs_in"], "")
        self.assertEqual(kwargs["srs_out"], "")

    def test_pc_tile_3d_omits_srs_for_empty_crs(self):
        p_py = self._run_tile3d_with_crs("")
        kwargs = p_py.return_value.build_cmd.call_args.kwargs
        self.assertEqual(kwargs["srs_in"], "")
        self.assertEqual(kwargs["srs_out"], "")

    # 9. pc_tile_3d 重跑前清空本阶段输出目录（py3dtiles 无增量续传）
    def test_pc_tile_3d_clears_output_dir_before_rerun(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="清目录",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="tile_3d",
                pc_crs="local",
            ),
            total=1,
        )
        out_dir = Path(get_task(task_id)["output_path"])
        stale = out_dir / "3dtiles" / "garbage.txt"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("old")

        def fake_py_run(cmd, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_py.return_value.run.side_effect = fake_py_run
        self._run(task_id)

        self.assertFalse(stale.exists())
        self.assertTrue((out_dir / "3dtiles" / "tileset.json").exists())
        self.assertEqual(get_task(task_id)["status"], "done")

    # 9b. convert_3d 同样重跑前清空 3dtiles/（fanvanzh 无增量语义），只清本阶段产物
    def test_convert_3d_clears_output_dir_before_rerun(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        osgb_dir = self.tmp / "osgb"
        (osgb_dir / "Tile_005").mkdir(parents=True)
        (osgb_dir / "Tile_005" / "Tile_005.osgb").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="清目录OSGB",
                provider="local_osgb",
                bbox=BBOX,
                source_path=str(osgb_dir),
                export="tile_3d",
            ),
            total=1,
        )
        out_dir = Path(get_task(task_id)["output_path"])
        stale = out_dir / "3dtiles" / "garbage.txt"
        stale.parent.mkdir(parents=True, exist_ok=True)
        stale.write_text("old")
        # 相邻成果(dem/dsm tif 等)不属于本阶段,重跑不得误删
        stray_tif = out_dir / "清目录OSGB_dsm.tif"
        stray_tif.write_bytes(b"tif")

        def fake_run(cmd, **kwargs):
            out = Path(kwargs["out_dir"])
            out.mkdir(parents=True, exist_ok=True)
            (out / "tileset.json").write_text("{}")
            return self._ok_result(out / "tileset.json")

        p_osgb.return_value.run.side_effect = fake_run
        self._run(task_id)

        self.assertFalse(stale.exists())
        self.assertTrue((out_dir / "3dtiles" / "tileset.json").exists())
        self.assertTrue(stray_tif.exists())
        self.assertEqual(get_task(task_id)["status"], "done")

    # 10. 目录多 LAS：逐文件转临时 tif 后 rasterio merge 成一幅成果
    def test_multi_las_directory_dsm_merges_parts(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src_dir = self.tmp / "las_dir"
        src_dir.mkdir()
        (src_dir / "a.las").write_bytes(b"x")
        (src_dir / "b.las").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="多文件DSM",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src_dir),
                export="dsm",
                pc_resolution=1.0,
            ),
            total=2,
        )

        def fake_pipeline(**kwargs):
            out = Path(kwargs["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            # 分幅命名带序号前缀（001_a.tif / 002_b.tif）
            self._write_tif(out, 0.0 if out.stem.endswith("_a") else 2.0)
            return self._ok_result(out)

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "done", msg=str(final.get("stages")))
        self.assertEqual(p_dem.return_value.run_pipeline.call_count, 2)
        out_tif = Path(final["output_path"]) / "多文件DSM_dsm.tif"
        with rasterio.open(out_tif) as ds:
            arr = ds.read()
            # 合并成果带压缩/分块工程参数（BIGTIFF 不是数据集属性读不回）
            self.assertEqual(ds.profile.get("compress"), "lzw")
            self.assertTrue(ds.profile.get("tiled"))
        # 两幅 2x2 横向相邻（x: 0..2 与 2..4）→ 合并后 2 行 4 列
        self.assertEqual(arr.shape, (1, 2, 4))
        # 临时分幅目录已清理
        self.assertFalse((Path(final["output_path"]) / "_dsm_parts").exists())

    # 10b. 目录递归收集时同名 LAS 靠序号前缀分幅命名,不互相覆盖
    def test_multi_las_same_stem_no_overwrite(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        src_dir = self.tmp / "las_dir"
        for flight in ("flight1", "flight2"):
            d = src_dir / flight
            d.mkdir(parents=True)
            (d / "data.las").write_bytes(b"x")
        task_id = create_task(
            TaskCreate(
                name="同名LAS",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src_dir),
                export="dsm",
                pc_resolution=1.0,
            ),
            total=2,
        )
        outputs: list[str] = []

        def fake_pipeline(**kwargs):
            out = Path(kwargs["output"])
            outputs.append(out.name)
            out.parent.mkdir(parents=True, exist_ok=True)
            # 第 1 个文件 x∈[0,2)、第 2 个 x∈[2,4)
            self._write_tif(out, 2.0 * (len(outputs) - 1))
            return self._ok_result(out)

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "done", msg=str(final.get("stages")))
        # 两个同名 data.las 的分幅输出名带序号前缀,互不相同
        self.assertEqual(len(set(outputs)), 2)
        self.assertTrue(all(n[:3].isdigit() for n in outputs), msg=str(outputs))
        out_tif = Path(final["output_path"]) / "同名LAS_dsm.tif"
        with rasterio.open(out_tif) as ds:
            arr = ds.read()
        # 两幅都进了合并(若互覆则只剩一幅,宽度只有 2)
        self.assertEqual(arr.shape, (1, 2, 4))

    # 11. pc_resolution=0 时由 preflight 的 bbox/points 自动估算
    def test_resolution_estimated_from_preflight_when_zero(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        p_dem.return_value.preflight.return_value = {
            "points": 40000,
            "bbox": {"minx": 0.0, "miny": 0.0, "maxx": 200.0, "maxy": 200.0},
            "srs": "",
            "warnings": [],
        }
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="自动分辨率",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="dsm",
                pc_resolution=0,
            ),
            total=1,
        )

        def fake_pipeline(**kwargs):
            out = Path(kwargs["output"])
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_bytes(b"tif")
            return self._ok_result(out)

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline
        self._run(task_id)

        self.assertEqual(get_task(task_id)["status"], "done")
        kwargs = p_dem.return_value.run_pipeline.call_args.kwargs
        # area=200*200=40000，points=40000 → res=sqrt(1)=1.0
        self.assertAlmostEqual(kwargs["resolution"], 1.0, places=3)

    # 12. 工具不可用（check_available=False）→ 阶段直接 fail，消息来自 check
    def test_stage_fails_with_check_available_message(self):
        p_osgb, p_dem, p_py = self._patch_processors()
        p_py.return_value.check_available.return_value = (
            False,
            "未配置 py3dtiles_python",
        )
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name="工具缺失",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="tile_3d",
            ),
            total=1,
        )
        self._run(task_id)

        final = get_task(task_id)
        self.assertEqual(final["status"], "failed")
        stages = {s["key"]: s for s in final["stages"]}
        self.assertEqual(stages["pc_tile_3d"]["status"], "failed")
        self.assertIn("未配置 py3dtiles_python", stages["pc_tile_3d"]["message"])

    def _run_with_watcher(self, control: str):
        """watcher 开启下跑一个会阻塞在 cancel_event 上的 dsm 任务。

        fake_pipeline 阻塞等 watcher 把控制标志桥接进 cancel_event;
        另一线程在 pipeline 进入后往 _control 写 pause/cancel。
        """
        p_osgb, p_dem, p_py = self._patch_processors(enable_watcher=True)
        src = _mk_src(self.tmp)
        task_id = create_task(
            TaskCreate(
                name=f"watcher{control}",
                provider="local_pointcloud",
                bbox=BBOX,
                source_path=str(src),
                export="dsm",
                pc_resolution=1.0,
            ),
            total=1,
        )
        entered = threading.Event()

        def fake_pipeline(**kwargs):
            entered.set()
            # 阻塞至 watcher 置位(5s 护栏防测试挂死)
            if kwargs["cancel_event"].wait(timeout=5.0):
                raise ProcessorCancelled("pdal")
            raise AssertionError("watcher 未在 5s 内置位 cancel_event")

        p_dem.return_value.run_pipeline.side_effect = fake_pipeline

        def _stop():
            if entered.wait(timeout=5.0):
                _control[task_id] = control

        t = threading.Thread(target=_stop, daemon=True)
        t.start()
        try:
            t0 = time.monotonic()
            self._run(task_id)
            # 0.5s 轮询,正常 1~2s 内响应;超时说明 watcher 通路断了
            self.assertLess(time.monotonic() - t0, 5.0)
        finally:
            _control.pop(task_id, None)
            t.join(timeout=6.0)
        return task_id

    # 13. watcher 是生产暂停唯一通路:request_pause → cancel_event → paused
    def test_watcher_pause_lands_paused(self):
        task_id = self._run_with_watcher("pause")
        self.assertEqual(get_task(task_id)["status"], "paused")
        # watcher 线程随 run_task 结束 join,不泄漏
        alive = {t.name for t in threading.enumerate()}
        self.assertNotIn(f"runner3d-watch-{task_id[:8]}", alive)

    # 14. watcher 取消通路:request_cancel → canceled
    def test_watcher_cancel_lands_canceled(self):
        task_id = self._run_with_watcher("cancel")
        self.assertEqual(get_task(task_id)["status"], "canceled")
        alive = {t.name for t in threading.enumerate()}
        self.assertNotIn(f"runner3d-watch-{task_id[:8]}", alive)


# 原 QueueDispatchCase(queue._resolve_runner 按 provider 分发)已删除:
# TaskQueue 与它的 _resolve_runner 在进程隔离改造中一并移除,分发逻辑迁到
# worker.py::_resolve_runner,覆盖由 tests/test_worker_dispatch.py 的
# TestWorkerDispatch 承接(同样是 3D 两个分支 + 非 3D 回落栅格 runner)。


if __name__ == "__main__":
    unittest.main()
