"""外部三维处理器诊断接口:提交三维任务前自检工具链是否可用。

逐项检查 config.yaml 里 tools 段配置的路径:是否存在、能否启动、版本输出。
与 local.py 同前提(本机自用),故同样要求请求来自回环地址——这里会按配置
路径启动本机可执行文件。
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, Request

from ..config import settings
from .local import _require_local

router = APIRouter(prefix="/api/tools", tags=["tools"])

#: 试跑超时(秒)。--version/-h 都是即时退出的命令,超时即视为异常。
_PROBE_TIMEOUT = 5

#: 待诊断的工具:(结果键名, ToolsConfig 字段名)。dsm2dtm_python 一期不用,不诊断。
_TOOLS = [
    ("tiles3d", "tiles3d_exe"),
    ("pdal", "pdal_exe"),
    ("py3dtiles", "py3dtiles_python"),
]

#: 适配层 check_available 支持 PATH 裸命令名(shutil.which)的工具字段。
#: 诊断口径必须与适配层一致:config 里填 "pdal" 时 las_to_dem 能跑,
#: 这里就不能误报"路径不存在"。另两个工具的适配层只认文件路径,同样不回落。
_PATH_FALLBACK_ATTRS = {"pdal_exe"}

#: py3dtiles 配置的是独立 venv 的解释器,只验解释器能跑不代表模块已装;
#: 探针命令与退出码约定和适配层 las_to_3dtiles.check_available 保持一致。
_PY3DTILES_ATTR = "py3dtiles_python"
_PY3DTILES_PROBE = ["-m", "py3dtiles.command_line", "-h"]

# Windows 下避免试跑时弹出控制台黑窗
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _first_line(text: str) -> str:
    """取输出里第一个非空行(截断),作为版本/标识信息。"""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _try_run(argv: list[str]) -> tuple[bool, str, str, int | None]:
    """启动一次进程,返回 (是否在超时内正常退出, 合并输出, 错误描述, 退出码)。"""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            timeout=_PROBE_TIMEOUT, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "", f"试跑超时({_PROBE_TIMEOUT}s 未退出)", None
    except OSError as e:
        return False, "", f"无法启动:{e}", None
    out = (proc.stdout or "") + (proc.stderr or "")
    return True, out, "", proc.returncode


def _diagnose_one(attr: str) -> dict:
    """诊断单个工具:未配置直接返回;已配置则查存在性并试跑 --version/-h。"""
    raw = (getattr(settings.tools, attr, "") or "").strip().strip('"')
    item = {"configured": bool(raw), "path": "", "exists": False,
            "runnable": False, "version": "", "error": ""}
    if not raw:
        return item

    p = settings.abs_path(raw)
    if not p.is_file() and attr in _PATH_FALLBACK_ATTRS:
        # 允许只填命令名(如 "pdal"),从 PATH 解析;命中后以解析出的绝对路径
        # 继续后续检查,诊断结果的 path 即实际命中的可执行文件
        found = shutil.which(raw)
        if found:
            p = Path(found)
    item["path"] = str(p)
    if not p.is_file():
        item["error"] = "路径不存在或不是文件"
        return item
    item["exists"] = True

    if attr == _PY3DTILES_ATTR:
        return _diagnose_py3dtiles(item, p)

    # 有的工具不认 --version(打印用法或静默忽略),无输出时再试 -h。
    # runnable 定义为"能启动并在超时内退出":fanvanzh/3dtiles 这类工具
    # 打用法信息时退出码非 0,不能按退出码判断。
    for args in (["--version"], ["-h"]):
        exited, out, err, _code = _try_run([str(p), *args])
        if not exited:
            item["error"] = err
            return item
        line = _first_line(out)
        if line:
            item["runnable"] = True
            item["version"] = line
            return item
    # 两次都无输出:进程能跑但没有任何打印,仍算可启动,只是拿不到版本
    item["runnable"] = True
    item["error"] = "可启动但 --version/-h 均无输出,无法确认版本"
    return item


def _diagnose_py3dtiles(item: dict, python: Path) -> dict:
    """py3dtiles 特判:除解释器可启动外,还必须确认该 venv 里装了 py3dtiles 模块。

    探针 `python -m py3dtiles.command_line -h` 与退出码约定同适配层
    (tests/test_las_to_3dtiles.py 锁定):退出码 0 才算可用,非 0 的典型
    原因就是解释器在但模块没装。
    """
    exited, out, err, code = _try_run([str(python), *_PY3DTILES_PROBE])
    if not exited:
        item["error"] = err
        return item
    if code != 0:
        item["error"] = ("解释器可用但未安装 py3dtiles"
                         "(应在该 venv 中执行 pip install py3dtiles)")
        return item
    item["runnable"] = True
    item["version"] = _first_line(out)
    return item


@router.get("/diagnose")
async def api_tools_diagnose(request: Request):
    """逐项诊断外部三维处理器,返回 {name: {configured, path, exists, runnable, version, error}}。

    前端提交三维任务前调用:任一环节 configured/exists/runnable 为 False
    都应先提示用户去修配置,而不是把任务排进队列再失败。
    """
    _require_local(request)
    result = {}
    for name, attr in _TOOLS:
        result[name] = await asyncio.to_thread(_diagnose_one, attr)
    return result
