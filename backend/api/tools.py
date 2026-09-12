"""外部三维处理器诊断接口:提交三维任务前自检工具链是否可用。

逐项检查 config.yaml 里 tools 段配置的路径:是否存在、能否启动、版本输出。
与 local.py 同前提(本机自用),故同样要求请求来自回环地址——这里会按配置
路径启动本机可执行文件。
"""
from __future__ import annotations

import asyncio
import subprocess
import sys

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

# Windows 下避免试跑时弹出控制台黑窗
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0


def _first_line(text: str) -> str:
    """取输出里第一个非空行(截断),作为版本/标识信息。"""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:200]
    return ""


def _try_run(argv: list[str]) -> tuple[bool, str, str]:
    """启动一次进程,返回 (是否在超时内正常退出, 合并输出, 错误描述)。"""
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, errors="replace",
            timeout=_PROBE_TIMEOUT, creationflags=_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return False, "", f"试跑超时({_PROBE_TIMEOUT}s 未退出)"
    except OSError as e:
        return False, "", f"无法启动:{e}"
    out = (proc.stdout or "") + (proc.stderr or "")
    return True, out, ""


def _diagnose_one(attr: str) -> dict:
    """诊断单个工具:未配置直接返回;已配置则查存在性并试跑 --version/-h。"""
    raw = (getattr(settings.tools, attr, "") or "").strip().strip('"')
    item = {"configured": bool(raw), "path": "", "exists": False,
            "runnable": False, "version": "", "error": ""}
    if not raw:
        return item

    p = settings.abs_path(raw)
    item["path"] = str(p)
    if not p.is_file():
        item["error"] = "路径不存在或不是文件"
        return item
    item["exists"] = True

    # 有的工具不认 --version(打印用法或静默忽略),无输出时再试 -h。
    # runnable 定义为"能启动并在超时内退出":fanvanzh/3dtiles 这类工具
    # 打用法信息时退出码非 0,不能按退出码判断。
    for args in (["--version"], ["-h"]):
        exited, out, err = _try_run([str(p), *args])
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
