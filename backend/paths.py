"""双根路径解析:区分只读资源根与可写运行根。

打包(PyInstaller onedir)后:
  - 资源根(resource_root):随 exe 一起打包的只读资源(前端 dist、离线底图、
    config.example.yaml、GDAL/proj 数据)。onedir 下 sys._MEIPASS 指向 exe 同级
    的 _internal 解包目录。
  - 运行根(runtime_root):exe 所在目录,存放可写数据(config.yaml、output/、
    data/、SQLite 库、tk 池)。

开发环境(未打包)下两者都回落到项目根(backend 的上一级),行为与打包前完全一致。
"""
from __future__ import annotations

import sys
from pathlib import Path

# 项目根:本文件在 backend/ 下,上一级即项目根
_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def is_frozen() -> bool:
    """是否运行在 PyInstaller 冻结环境中。"""
    return getattr(sys, "frozen", False)


def resource_root() -> Path:
    """只读资源根。冻结时为 _MEIPASS(打包资源解包目录),否则为项目根。"""
    if is_frozen():
        base = getattr(sys, "_MEIPASS", None)
        if base:
            return Path(base)
    return _PROJECT_ROOT


def runtime_root() -> Path:
    """可写运行根。冻结时为 exe 所在目录,否则为项目根。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return _PROJECT_ROOT
