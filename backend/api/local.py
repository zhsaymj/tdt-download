"""本地数据入库相关接口:选文件、检查文件、列出可用导出格式。

设计前提是**本机自用**(服务绑 127.0.0.1)。这里的接口能弹出系统文件对话框、
读取任意本地路径,故全部要求请求来自本机——若哪天改成局域网共享,这些接口必须
先关掉或加真正的鉴权。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Literal

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
    kind: Literal["raster", "vector", "pointcloud", "dir"] = Field(
        default="raster", description="raster | vector | pointcloud | dir")
    multiple: bool = Field(default=True)
    initial_dir: str = Field(default="", description="对话框初始目录")


@router.post("/pick")
async def api_pick(request: Request, data: PickReq):
    """弹出系统文件/目录对话框,返回选中路径的真实路径。

    POST 而非 GET:它有副作用(弹出一个模态窗口、阻塞等待用户操作),
    不该被浏览器预取或缓存。kind="dir" 用于 OSGB 模型目录这类
    "输入是一个目录"的数据源;kind="pointcloud" 按 LAS/LAZ 过滤。
    """
    _require_local(request)
    try:
        if data.kind == "dir":
            paths = await file_dialog.pick(
                kind="dir", title="选择数据目录",
                initial_dir=data.initial_dir or None)
        else:
            patterns = {
                "vector": file_dialog.VECTOR_PATTERNS,
                "pointcloud": file_dialog.POINTCLOUD_PATTERNS,
            }.get(data.kind, file_dialog.RASTER_PATTERNS)
            title = {
                "vector": "选择矢量文件",
                "pointcloud": "选择点云文件",
            }.get(data.kind, "选择栅格文件")
            paths = await file_dialog.pick(
                kind="file", title=title, patterns=patterns,
                multiple=data.multiple,
                initial_dir=data.initial_dir or None)
    except RuntimeError as e:
        raise HTTPException(500, str(e)) from e
    return {"paths": paths}


class InspectReq(BaseModel):
    path: str = Field(..., description="本地文件/目录的绝对路径")


def _checked_existing(raw: str) -> Path:
    """校验并规整用户给的路径(粘贴时常带引号),要求绝对且存在。"""
    p = Path((raw or "").strip().strip('"'))
    if not p.is_absolute():
        raise HTTPException(400, "请提供绝对路径")
    if not p.exists():
        raise HTTPException(404, f"路径不存在:{p}")
    return p


def _checked_file(raw: str) -> Path:
    """在 _checked_existing 之上要求必须是文件。"""
    p = _checked_existing(raw)
    if not p.is_file():
        raise HTTPException(400, f"不是文件:{p}")
    return p


def _checked_dir(raw: str) -> Path:
    """在 _checked_existing 之上要求必须是目录。"""
    p = _checked_existing(raw)
    if not p.is_dir():
        raise HTTPException(400, f"不是目录:{p}")
    return p


def _inspect_osgb_dir(p: Path) -> dict:
    """OSGB 目录提交前检查(同步,放线程里跑:rglob 大目录会阻塞事件循环)。

    有效结构:目录内(递归)至少一个 .osgb;metadata.xml 缺失不致命,
    但模型坐标参考可能不全,经 warning 字段提示用户。
    """
    has_osgb = any(f.suffix.lower() == ".osgb"
                   for f in p.rglob("*") if f.is_file())
    if not has_osgb:
        raise HTTPException(400, f"目录下没有找到 .osgb 文件:{p}")
    warning = None
    if not (p / "metadata.xml").is_file():
        warning = ("目录下缺少 metadata.xml:模型坐标参考可能缺失,"
                   "转换后的 3D Tiles 可能无法正确落点。")
    return {"path": str(p), "warning": warning}


@router.post("/inspect_osgb")
async def api_inspect_osgb(request: Request, data: InspectReq):
    """检查 OSGB 倾斜模型目录:是否含 .osgb 数据,缺 metadata.xml 给 warning。"""
    _require_local(request)
    p = _checked_dir(data.path)
    return await asyncio.to_thread(_inspect_osgb_dir, p)


def _inspect_pointcloud(p: Path, cfg) -> dict:
    """点云提交前预检(同步,放线程里跑:目录枚举与 pdal info 都是阻塞操作)。

    目录输入时 files 给递归 LAS/LAZ 清单,但一期只对排序后的第一个文件做
    pdal 预检(与 LasToDem 单文件支持一致)。
    「无 LAS 文件」「pdal 不可用」「preflight 失败」都不抛 HTTP 错,
    经 error 字段返回中文原因(HTTP 仍 200),前端按 error 是否为空分支;
    只有路径本身非法(非绝对/不存在)才在路由层 4xx。
    """
    from ..core.processors.base import ProcessorError
    from ..core.processors.las_to_dem import LasToDem

    result = {"files": [], "count": None, "bbox": None, "srs": None,
              "error": None}
    if p.is_dir():
        files = sorted(f for f in p.rglob("*")
                       if f.is_file() and f.suffix.lower() in (".las", ".laz"))
        if not files:
            result["error"] = f"目录下没有找到 las/laz 点云文件:{p}"
            return result
    elif p.suffix.lower() in (".las", ".laz"):
        files = [p]
    else:
        result["error"] = f"点云数据源只支持 las/laz 文件:{p}"
        return result

    result["files"] = [f.name for f in files]
    try:
        info = LasToDem().preflight(files[0], cfg)
    except ProcessorError as e:
        result["error"] = str(e)
        return result
    result["count"] = info["points"]
    result["bbox"] = info["bbox"]
    # srs 空串归一为 None:前端据 srs is null 提示用户手选 EPSG 或按本地坐标
    result["srs"] = info["srs"] or None
    return result


@router.post("/inspect_pointcloud")
async def api_inspect_pointcloud(request: Request, data: InspectReq):
    """检查本地 LAS/LAZ 点云:文件清单、点数、范围与 CRS,供提交前确认。

    pdal info 是子进程调用,放线程池执行避免阻塞事件循环。
    """
    _require_local(request)
    from ..config import settings

    p = _checked_existing(data.path)
    return await asyncio.to_thread(_inspect_pointcloud, p, settings)


@router.post("/inspect_vector")
async def api_inspect_vector(request: Request, data: InspectReq):
    """检查本地矢量文件:几何类型、要素数、字段、CRS、可转的容器格式。

    与 /inspect(栅格)分开:矢量只做容器转换、没有"处理阶段"的概念,
    返回结构与用途都不同。
    """
    _require_local(request)
    from ..core import local_vector_file

    p = _checked_file(data.path)
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

    p = _checked_file(data.path)
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

    p = _checked_file(data.path)
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



class RevealReq(BaseModel):
    path: str = Field(..., description="要在文件管理器中打开的路径")


@router.post("/reveal")
async def api_reveal(request: Request, data: RevealReq):
    """在系统文件管理器中打开成果目录(或选中成果文件)。

    只放行 output 目录内的路径。这个接口会让操作系统去"打开"一个路径,若不加
    限制就等于给了任意路径的启动入口——Windows 上 startfile 一个 .bat/.exe
    会直接执行它。故先 resolve()(解掉符号链接与 ..)再比对是否在 output 内。
    """
    _require_local(request)
    import os
    import sys

    from ..config import settings

    out_root = settings.output_dir.resolve()
    try:
        p = Path((data.path or "").strip().strip('"')).resolve()
    except OSError as e:
        raise HTTPException(400, f"路径无效:{e}") from e
    if not p.is_relative_to(out_root):
        raise HTTPException(403, "只能打开成果目录内的路径")
    if not p.exists():
        raise HTTPException(404, f"路径不存在:{p}")

    # 目标是文件时打开它所在的目录(用户要的是"看到成果",不是用默认程序打开 tif)
    target = p if p.is_dir() else p.parent
    try:
        if sys.platform == "win32":
            await asyncio.to_thread(os.startfile, str(target))
        elif sys.platform == "darwin":
            proc = await asyncio.create_subprocess_exec("open", str(target))
            await proc.wait()
        else:
            proc = await asyncio.create_subprocess_exec("xdg-open", str(target))
            await proc.wait()
    except Exception as e:
        raise HTTPException(500, f"打开目录失败:{str(e)[:200]}") from e
    return {"opened": str(target)}
