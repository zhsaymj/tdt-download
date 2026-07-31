"""本地数据入库相关接口:选文件、检查文件、列出可用导出格式。

设计前提是**本机自用**(服务绑 127.0.0.1)。这里的接口能弹出系统文件对话框、
读取任意本地路径,故全部要求请求来自本机——若哪天改成局域网共享,这些接口必须
先关掉或加真正的鉴权。
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..core import file_dialog
from ..core.logs import logger

router = APIRouter(prefix="/api/local", tags=["local"])

#: 视为本机的来源地址。IPv6 回环与 IPv4 映射都要算上。
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1"}


def _require_local(request: Request) -> None:
    """拒绝非本机请求。

    这些接口能读本机任意文件,只在"前后端同机"的前提下才是安全的。服务默认绑
    127.0.0.1 已经拦住了外部访问,但配置里允许改成 0.0.0.0(供同网段访问界面),
    那种情况下必须靠这里拦住。
    """
    host = (request.client.host if request.client else "") or ""
    if host not in _LOOPBACK:
        logger.warning("拒绝非本机的本地文件访问请求:%s", host)
        raise HTTPException(
            403, "该功能仅限本机使用(涉及读取本地文件)。当前请求来自 " + host)


@router.get("/dialog_available")
async def api_dialog_available(request: Request):
    """系统文件对话框是否可用(不可用时界面回落为手工填路径)。"""
    _require_local(request)
    return {"available": await asyncio.to_thread(file_dialog.dialog_available)}


class PickReq(BaseModel):
    kind: str = Field(default="raster", description="raster | vector")
    multiple: bool = Field(default=True)
    initial_dir: str = Field(default="", description="对话框初始目录")


@router.post("/pick")
async def api_pick(request: Request, data: PickReq):
    """弹出系统文件对话框,返回选中文件的真实路径。

    POST 而非 GET:它有副作用(弹出一个模态窗口、阻塞等待用户操作),
    不该被浏览器预取或缓存。
    """
    _require_local(request)
    patterns = (file_dialog.VECTOR_PATTERNS if data.kind == "vector"
                else file_dialog.RASTER_PATTERNS)
    title = "选择矢量文件" if data.kind == "vector" else "选择栅格文件"
    try:
        paths = await file_dialog.pick_files(
            title=title, patterns=patterns, multiple=data.multiple,
            initial_dir=data.initial_dir or None)
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    return {"paths": paths}


class InspectReq(BaseModel):
    path: str = Field(..., description="本地栅格文件的绝对路径")


def _checked_path(raw: str) -> Path:
    """校验并规整用户给的路径(粘贴时常带引号)。"""
    p = Path((raw or "").strip().strip('"'))
    if not p.is_absolute():
        raise HTTPException(400, "请提供绝对路径")
    if not p.exists():
        raise HTTPException(404, f"文件不存在:{p}")
    if not p.is_file():
        raise HTTPException(400, f"不是文件:{p}")
    return p


@router.post("/inspect_vector")
async def api_inspect_vector(request: Request, data: InspectReq):
    """检查本地矢量文件:几何类型、要素数、字段、CRS、可转的容器格式。

    与 /inspect(栅格)分开:矢量只做容器转换、没有"处理阶段"的概念,
    返回结构与用途都不同。
    """
    _require_local(request)
    from ..core import local_vector_file

    p = _checked_path(data.path)
    try:
        return await asyncio.to_thread(local_vector_file.inspect_vector, p)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(
            400, f"无法读取该矢量({type(e).__name__}):{str(e)[:200]}") from e


class ConvertVectorReq(BaseModel):
    path: str = Field(..., description="源矢量文件的绝对路径")
    container: str = Field(..., description="目标容器:geojson / gpkg / shapefile")
    name: str = Field(default="", description="成果名(默认取源文件名)")


@router.post("/convert_vector")
async def api_convert_vector(request: Request, data: ConvertVectorReq):
    """把本地矢量转成指定容器格式,输出到独立的成果目录。

    不走任务队列:矢量转换是**单步、快速**的(没有下载、拼接、切片),
    排进队列反而让用户多等一次调度、还要去任务列表里找结果。
    大文件也就几秒——实测 9 万要素的 GeoJSON 转 GPKG 在秒级。
    """
    _require_local(request)
    from ..core import local_vector_file
    from ..models import reserve_output_dir, safe_dirname
    from ..core.formats import CONTAINERS

    p = _checked_path(data.path)
    cont = CONTAINERS.get(data.container)
    if cont is None or cont.writer != "pyogrio":
        raise HTTPException(400, f"不支持的矢量容器格式:{data.container}")

    stem = safe_dirname(data.name or p.stem)
    out_dir = await asyncio.to_thread(reserve_output_dir, stem)
    dst = out_dir / f"{stem}{cont.ext}"
    try:
        await asyncio.to_thread(local_vector_file.convert_vector, p, dst,
                                data.container)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"转换失败:{str(e)[:250]}") from e

    files = sorted(f.name for f in out_dir.iterdir() if f.is_file())
    return {"output_dir": str(out_dir), "output": str(dst), "files": files}


@router.post("/inspect")
async def api_inspect(request: Request, data: InspectReq):
    """检查本地栅格:返回元信息、判定出的数据类型、可用的导出格式。

    判定不确定时(如 int16 既可能是高程也可能是 16bit 影像)仍返回一个 kind,
    但 kind_confident 为 false,由界面提示用户确认。
    """
    _require_local(request)
    from ..core import local_raster

    p = _checked_path(data.path)
    try:
        info = await asyncio.to_thread(local_raster.inspect_for_import, p)
    except ValueError as e:
        # inspect 里对"无 CRS""坐标系无法转换"这类会抛 ValueError,是用户可修的问题
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(
            400, f"无法读取该栅格({type(e).__name__}):{str(e)[:200]}") from e

    info["path"] = str(p)
    info["filename"] = p.name
    try:
        info["bytes"] = p.stat().st_size
    except OSError:
        info["bytes"] = 0
    return info

