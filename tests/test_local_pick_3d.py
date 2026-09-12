"""本地文件选择扩展测试:目录对话框、OSGB 目录检查、点云提交前预检接口。

tkinter 全程用 sys.modules 注入假模块,不弹真实对话框;
LasToDem.preflight 用 mock 替身,不依赖真实 pdal 与 LAS 样例。
"""
import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest import mock

from fastapi import HTTPException

from backend.core import file_dialog
from backend.core.processors.base import ProcessorError


def _local_request():
    """构造来自本机的假 Request(_require_local 只读 client.host)。"""
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def _fake_tkinter(**answers):
    """构造假 tkinter 模块:按调用顺序记录对话框调用,按 answers 给返回值。"""
    tk = ModuleType("tkinter")
    fd = ModuleType("tkinter.filedialog")
    calls = []

    class _Root:
        def withdraw(self): pass
        def attributes(self, *a, **k): pass
        def lift(self): pass
        def focus_force(self): pass
        def update(self): pass
        def destroy(self): pass

    tk.Tk = _Root

    def _recorder(name):
        def _fn(**kw):
            calls.append((name, kw))
            return answers.get(name, "")
        return _fn

    fd.askdirectory = _recorder("askdirectory")
    fd.askopenfilename = _recorder("askopenfilename")
    fd.askopenfilenames = _recorder("askopenfilenames")
    tk.filedialog = fd
    return {"tkinter": tk, "tkinter.filedialog": fd}, calls


class PickDialogTest(unittest.TestCase):
    """file_dialog.pick:kind 分发 file/dir,点云过滤 LAS/LAZ。"""

    def test_pick_dir_uses_askdirectory(self):
        fake, calls = _fake_tkinter(askdirectory="D:/data/osgb模型")
        with mock.patch.dict(sys.modules, fake):
            paths = asyncio.run(file_dialog.pick(kind="dir", title="选择数据目录"))
        self.assertEqual(paths, ["D:/data/osgb模型"])
        self.assertEqual([c[0] for c in calls], ["askdirectory"])

    def test_pick_dir_cancel_returns_empty(self):
        fake, calls = _fake_tkinter(askdirectory="")
        with mock.patch.dict(sys.modules, fake):
            paths = asyncio.run(file_dialog.pick(kind="dir"))
        self.assertEqual(paths, [])

    def test_pick_pointcloud_filters_las_laz(self):
        fake, calls = _fake_tkinter(askopenfilenames=("D:/a.las", "D:/b.laz"))
        with mock.patch.dict(sys.modules, fake):
            paths = asyncio.run(file_dialog.pick(
                kind="file", title="选择点云文件",
                patterns=file_dialog.POINTCLOUD_PATTERNS))
        self.assertEqual(paths, ["D:/a.las", "D:/b.laz"])
        self.assertEqual(calls[0][0], "askopenfilenames")
        self.assertIn(("LAS/LAZ 点云", "*.las *.laz"), calls[0][1]["filetypes"])

    def test_pick_file_still_supports_raster_patterns(self):
        # 既有行为不回归:字符串 patterns 走「支持的文件」过滤
        fake, calls = _fake_tkinter(askopenfilename="D:/a.tif")
        with mock.patch.dict(sys.modules, fake):
            paths = asyncio.run(file_dialog.pick(
                kind="file", patterns=file_dialog.RASTER_PATTERNS,
                multiple=False))
        self.assertEqual(paths, ["D:/a.tif"])
        self.assertEqual(calls[0][0], "askopenfilename")
        self.assertIn(("支持的文件", file_dialog.RASTER_PATTERNS),
                      calls[0][1]["filetypes"])


class PickApiTest(unittest.TestCase):
    """POST /api/local/pick 的 kind 分发(直接调路由函数,假本机 Request)。"""

    def test_pick_req_kind_dir_dispatches_askdirectory(self):
        from backend.api.local import PickReq, api_pick

        fake, calls = _fake_tkinter(askdirectory="D:/data/osgb模型")
        with mock.patch.dict(sys.modules, fake):
            resp = asyncio.run(
                api_pick(_local_request(), PickReq(kind="dir")))
        self.assertEqual(resp["paths"], ["D:/data/osgb模型"])
        self.assertEqual([c[0] for c in calls], ["askdirectory"])

    def test_pick_req_kind_pointcloud_filters_las(self):
        from backend.api.local import PickReq, api_pick

        fake, calls = _fake_tkinter(askopenfilenames=("D:/a.las",))
        with mock.patch.dict(sys.modules, fake):
            resp = asyncio.run(
                api_pick(_local_request(), PickReq(kind="pointcloud")))
        self.assertEqual(resp["paths"], ["D:/a.las"])
        self.assertEqual(calls[0][0], "askopenfilenames")
        self.assertIn(("LAS/LAZ 点云", "*.las *.laz"), calls[0][1]["filetypes"])

    def test_pick_req_rejects_unknown_kind(self):
        from pydantic import ValidationError

        from backend.api.local import PickReq
        with self.assertRaises(ValidationError):
            PickReq(kind="exe")

    def test_pick_req_rejects_remote_client(self):
        from backend.api.local import PickReq, api_pick

        remote = SimpleNamespace(client=SimpleNamespace(host="192.168.1.10"))
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(api_pick(remote, PickReq(kind="dir")))
        self.assertEqual(cm.exception.status_code, 403)


class InspectOsgbTest(unittest.TestCase):
    """POST /api/local/inspect_osgb:目录内至少一个 .osgb,缺 metadata.xml 给 warning。"""

    def _make_tree(self, root: Path, with_metadata: bool = True):
        tile = root / "Data" / "Tile_000_000"
        tile.mkdir(parents=True)
        (tile / "Tile_000_000.osgb").write_bytes(b"fake-osgb")
        if with_metadata:
            (root / "metadata.xml").write_text("<ModelMetadata/>",
                                               encoding="utf-8")

    def _inspect(self, path: str):
        from backend.api.local import InspectReq, api_inspect_osgb
        return asyncio.run(
            api_inspect_osgb(_local_request(), InspectReq(path=path)))

    def test_ok_tree_has_no_warning(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(Path(d))
            resp = self._inspect(d)
        self.assertIsNone(resp["warning"])

    def test_missing_metadata_gives_warning(self):
        with tempfile.TemporaryDirectory() as d:
            self._make_tree(Path(d), with_metadata=False)
            resp = self._inspect(d)
        self.assertIn("metadata.xml", resp["warning"])

    def test_dir_without_osgb_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "readme.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(HTTPException) as cm:
                self._inspect(d)
        self.assertEqual(cm.exception.status_code, 400)

    def test_file_path_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "metadata.xml"
            f.write_text("<xml/>", encoding="utf-8")
            with self.assertRaises(HTTPException) as cm:
                self._inspect(str(f))
        self.assertEqual(cm.exception.status_code, 400)

    def test_nonexistent_path_404(self):
        with self.assertRaises(HTTPException) as cm:
            self._inspect("D:/no_such_dir_xyz_123")
        self.assertEqual(cm.exception.status_code, 404)


class InspectPointcloudTest(unittest.TestCase):
    """POST /api/local/inspect_pointcloud:{files, count, bbox, srs, error}。"""

    _INFO = {"points": 1234,
             "bbox": {"minx": 1.0, "miny": 2.0, "minz": 3.0,
                      "maxx": 4.0, "maxy": 5.0, "maxz": 6.0},
             "srs": "+proj=tmerc +lat_0=0 +lon_0=114",
             "warnings": []}

    def _inspect(self, path: str):
        from backend.api.local import InspectReq, api_inspect_pointcloud
        return asyncio.run(
            api_inspect_pointcloud(_local_request(), InspectReq(path=path)))

    def _patch_preflight(self, **kw):
        return mock.patch(
            "backend.core.processors.las_to_dem.LasToDem.preflight", **kw)

    def test_single_las_returns_preflight_info(self):
        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "tile_001.las"
            las.write_bytes(b"LASF")
            with self._patch_preflight(return_value=dict(self._INFO)) as pf:
                resp = self._inspect(str(las))
        pf.assert_called_once()
        self.assertIsNone(resp["error"])
        self.assertEqual(resp["files"], ["tile_001.las"])
        self.assertEqual(resp["count"], 1234)
        self.assertEqual(resp["bbox"]["maxx"], 4.0)
        self.assertEqual(resp["srs"], "+proj=tmerc +lat_0=0 +lon_0=114")

    def test_missing_srs_returns_null(self):
        # LAS 头无 CRS:srs 归一为 None,前端据此提示选 EPSG 或按本地坐标
        info = dict(self._INFO, srs="", warnings=["LAS 头部不含 SRS 坐标参考"])
        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "a.las"
            las.write_bytes(b"LASF")
            with self._patch_preflight(return_value=info):
                resp = self._inspect(str(las))
        self.assertIsNone(resp["error"])
        self.assertIsNone(resp["srs"])

    def test_directory_lists_las_and_preflights_first(self):
        # 目录输入:files 给递归相对路径清单(子目录文件带前缀,同名可区分),
        # preflight 只打排序后的第一个文件
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "b.laz").write_bytes(b"LASF")
            sub = root / "sub"
            sub.mkdir()
            (sub / "a.las").write_bytes(b"LASF")
            (root / "c.txt").write_text("x", encoding="utf-8")
            with self._patch_preflight(return_value=dict(self._INFO)) as pf:
                resp = self._inspect(d)
        self.assertEqual(resp["files"],
                         ["b.laz", str(Path("sub") / "a.las")])
        self.assertEqual(Path(pf.call_args.args[0]).name, "b.laz")
        self.assertIsNone(resp["error"])
        self.assertEqual(resp["count"], 1234)

    def test_directory_without_las_gives_error(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "readme.txt").write_text("x", encoding="utf-8")
            with self._patch_preflight(return_value=dict(self._INFO)) as pf:
                resp = self._inspect(d)
        pf.assert_not_called()
        self.assertEqual(resp["files"], [])
        self.assertIn("las", resp["error"])

    def test_pdal_unavailable_gives_error_but_keeps_files(self):
        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "a.las"
            las.write_bytes(b"LASF")
            err = ProcessorError(
                "pdal", None,
                hint="未配置 pdal 可执行文件路径(tools.pdal_exe),无法预检 LAS 头")
            with self._patch_preflight(side_effect=err):
                resp = self._inspect(str(las))
        self.assertEqual(resp["files"], ["a.las"])
        self.assertIsNone(resp["count"])
        self.assertIn("pdal", resp["error"])

    def test_preflight_failure_gives_error(self):
        with tempfile.TemporaryDirectory() as d:
            las = Path(d) / "broken.las"
            las.write_bytes(b"not-a-las")
            err = ProcessorError("pdal", 1, "bad file",
                                 hint="pdal info 读取 LAS 头失败")
            with self._patch_preflight(side_effect=err):
                resp = self._inspect(str(las))
        self.assertIn("失败", resp["error"])

    def test_non_las_file_gives_error(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "a.txt"
            f.write_text("x", encoding="utf-8")
            with self._patch_preflight(return_value=dict(self._INFO)) as pf:
                resp = self._inspect(str(f))
        pf.assert_not_called()
        self.assertIn("las", resp["error"])

    def test_nonexistent_path_404(self):
        with self.assertRaises(HTTPException) as cm:
            self._inspect("D:/no_such_file_xyz_123.las")
        self.assertEqual(cm.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main()
