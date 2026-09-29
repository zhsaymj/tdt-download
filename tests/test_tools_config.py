import tempfile
import unittest
from pathlib import Path

from backend.config import load_config


class ToolsConfigTest(unittest.TestCase):
    def test_default_tools_paths_empty(self):
        # 临时目录里不存在的配置路径 -> 全部取默认值
        with tempfile.TemporaryDirectory() as d:
            cfg = load_config(Path(d) / "config.yaml")
        self.assertEqual(cfg.tools.tiles3d_exe, "")
        self.assertEqual(cfg.tools.pdal_exe, "")
        self.assertEqual(cfg.tools.py3dtiles_python, "")
        self.assertEqual(cfg.tools.dsm2dtm_python, "")

    def test_tools_paths_merge_from_yaml(self):
        # yaml 中 tools 段应覆盖到 dataclass 对应字段
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "config.yaml"
            p.write_text(
                'tools:\n'
                '  tiles3d_exe: "tools/3dtiles/3dtiles.exe"\n'
                '  pdal_exe: "tools/pdal/bin/pdal.exe"\n',
                encoding="utf-8")
            cfg = load_config(p)
        self.assertEqual(cfg.tools.tiles3d_exe, "tools/3dtiles/3dtiles.exe")
        self.assertEqual(cfg.tools.pdal_exe, "tools/pdal/bin/pdal.exe")
        # 未配置的项仍为默认空串
        self.assertEqual(cfg.tools.py3dtiles_python, "")


if __name__ == "__main__":
    unittest.main()
