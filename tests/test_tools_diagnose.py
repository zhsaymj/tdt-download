"""tools 诊断接口(_diagnose_one)的单元测试。

覆盖两个与适配层口径对齐的修复:
1. pdal_exe 只填命令名(如 "pdal")时从 PATH 解析(shutil.which)——
   las_to_dem.check_available 支持,tiles3d/py3dtiles 的适配层只认文件路径,
   故后两者不回落(行为保持不变);
2. py3dtiles_python 除解释器可启动外,还必须确认该 venv 装了 py3dtiles 模块,
   探针 argv 与退出码约定同 las_to_3dtiles.check_available。

风格与 test_las_to_3dtiles 一致:能真跑的真跑(python.exe 副本作假 pdal、
sys.executable 作解释器、tmp 伪造 py3dtiles 包),探针 argv 与退出码→文案
分支用 mock 锁定。
"""

import importlib.util
import os
import shutil
import subprocess as sp
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from backend.api import tools as tools_api
from backend.config import settings

#: 当前解释器所属 venv 的 pyvenv.cfg(非 venv 运行时不存在)
_VENV_CFG = Path(sys.executable).parent.parent / "pyvenv.cfg"


class _ToolsCase(unittest.TestCase):
    """保存/恢复全局 settings.tools 单例字段,避免用例间串味。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self._old = {a: getattr(settings.tools, a)
                     for a in ("tiles3d_exe", "pdal_exe", "py3dtiles_python")}
        self.addCleanup(self._restore)

    def _restore(self):
        for attr, value in self._old.items():
            setattr(settings.tools, attr, value)


@unittest.skipUnless(_VENV_CFG.is_file(), "需要在 venv 解释器下运行(替身依赖 pyvenv.cfg)")
class PdalPathFallbackTest(_ToolsCase):
    """修复 1:pdal_exe 填裸命令名时,诊断与适配层一致从 PATH 解析。"""

    def _make_fake_exe(self) -> str:
        """tmp 下伪造一个真可执行的假 pdal,返回 (裸命令名, exe 所在目录)。

        直接复制 sys.executable(小 launcher):它从 exe 所在目录向上找
        pyvenv.cfg 定位 base 解释器的 DLL,故连同 venv 的 pyvenv.cfg 一起
        复制——这样副本 --version 能真跑出版本号。(Windows 系统 exe 副本
        会因 .mui 资源按名解析失败而静默,不能当替身。)
        """
        exe_dir = self.tmp / "bin"
        exe_dir.mkdir()
        shutil.copy2(sys.executable, exe_dir / "fakepdal_diag.exe")
        shutil.copy2(_VENV_CFG, self.tmp / "pyvenv.cfg")
        return "fakepdal_diag.exe", exe_dir

    def _path_with(self, exe_dir: Path) -> dict:
        return {"PATH": str(exe_dir) + os.pathsep + os.environ.get("PATH", "")}

    def test_pdal_bare_name_resolves_via_path(self):
        name, exe_dir = self._make_fake_exe()
        settings.tools.pdal_exe = name
        with mock.patch.dict(os.environ, self._path_with(exe_dir)):
            item = tools_api._diagnose_one("pdal_exe")
        self.assertTrue(item["configured"])
        self.assertTrue(item["exists"], msg=item["error"])
        self.assertTrue(item["runnable"], msg=item["error"])
        # 诊断结果能看出实际解析路径(PATH 命中的 tmp 副本),
        # 而不是按项目根解析出的 <ROOT>/fakepdal_diag.exe
        self.assertEqual(Path(item["path"]), exe_dir / name)
        self.assertIn("Python", item["version"])

    def test_tiles3d_does_not_fallback_to_path(self):
        # 口径对齐:仅 pdal 适配层支持 PATH;tiles3d(及 py3dtiles)只认文件路径,
        # 同样的裸命令名配置在 tiles3d 上必须仍报"路径不存在",不得回落
        name, exe_dir = self._make_fake_exe()
        settings.tools.tiles3d_exe = name
        with mock.patch.dict(os.environ, self._path_with(exe_dir)):
            item = tools_api._diagnose_one("tiles3d_exe")
        self.assertTrue(item["configured"])
        self.assertFalse(item["exists"])
        self.assertFalse(item["runnable"])
        self.assertEqual(item["error"], "路径不存在或不是文件")
        # path 仍是按项目根解析的结果,未被 PATH 命中路径替换
        self.assertEqual(Path(item["path"]), settings.abs_path(name))


class Py3dtilesProbeTest(_ToolsCase):
    """修复 3:py3dtiles_python 必须确认解释器里装了 py3dtiles 模块。"""

    def test_probe_argv_and_module_missing_message(self):
        settings.tools.py3dtiles_python = sys.executable
        with mock.patch(
                "backend.api.tools.subprocess.run",
                return_value=sp.CompletedProcess(
                    [], 1, stdout="", stderr="No module named py3dtiles")) as m:
            item = tools_api._diagnose_one("py3dtiles_python")
        # 探针 argv 与适配层一致:包内无 __main__.py,模块路径必须是
        # py3dtiles.command_line(该约定同样被 test_las_to_3dtiles 锁定)
        argv = m.call_args.args[0]
        self.assertEqual(
            argv[:4], [sys.executable, "-m", "py3dtiles.command_line", "-h"])
        self.assertTrue(item["exists"])
        self.assertFalse(item["runnable"])
        self.assertIn("未安装 py3dtiles", item["error"])
        self.assertIn("pip install", item["error"])

    def test_probe_success_with_fake_package(self):
        """真跑:tmp 伪造 py3dtiles 包,PYTHONPATH 注入后退出码 0 → runnable。"""
        pkg = self.tmp / "py3dtiles"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("")
        (pkg / "command_line.py").write_text(
            "import sys\n"
            "if __name__ == '__main__':\n"
            "    print('py3dtiles fake 1.0')\n"
            "    sys.exit(0)\n"
        )
        settings.tools.py3dtiles_python = sys.executable
        env = {"PYTHONPATH": str(self.tmp) + os.pathsep
               + os.environ.get("PYTHONPATH", "")}
        with mock.patch.dict(os.environ, env):
            item = tools_api._diagnose_one("py3dtiles_python")
        self.assertTrue(item["exists"], msg=item["error"])
        self.assertTrue(item["runnable"], msg=item["error"])
        self.assertIn("py3dtiles", item["version"])

    def test_probe_module_missing_real_run(self):
        """真跑:解释器在但模块没装 → 退出码非 0 → 中文提示去 pip install。"""
        if importlib.util.find_spec("py3dtiles") is not None:
            self.skipTest("当前解释器已装 py3dtiles,无法复现模块缺失分支")
        settings.tools.py3dtiles_python = sys.executable
        # PYTHONPATH 指向空 tmp,屏蔽外部环境里可能注入的 py3dtiles
        with mock.patch.dict(os.environ, {"PYTHONPATH": str(self.tmp)}):
            item = tools_api._diagnose_one("py3dtiles_python")
        self.assertTrue(item["exists"])
        self.assertFalse(item["runnable"])
        self.assertIn("未安装 py3dtiles", item["error"])


if __name__ == "__main__":
    unittest.main()
