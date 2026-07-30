"""三维建筑数据源(Overture)诊断接口。

Overture 托管在 AWS S3,能否访问受网络环境、匿名列举权限、发布版本号三者
影响,任务失败时光看日志难以定位是哪一环。这里提供两个只读接口,直接在
浏览器里就能判断:

  GET /api/buildings/diagnose   连通性 + 可用版本 + 当前选用版本
  GET /api/buildings/count      指定 bbox 的建筑数(验证谓词下推是否生效)
"""
from __future__ import annotations

import asyncio
import time

from fastapi import APIRouter, Body, HTTPException, Request

from ..config import settings
from ..core import vector_upload
from ..providers import overture

router = APIRouter(prefix="/api/buildings", tags=["buildings"])


def _proxy() -> str | None:
    bcfg = getattr(settings, "buildings", None)
    return (getattr(bcfg, "proxy", "") or None) if bcfg else None


def _configured_release() -> str | None:
    bcfg = getattr(settings, "buildings", None)
    return (getattr(bcfg, "release", "") or None) if bcfg else None


def _diagnose() -> dict:
    proxy = _proxy()
    configured = _configured_release()
    out: dict = {
        "proxy": proxy or "(未配置)",
        "configured_release": configured or "(未指定,自动探测)",
        "fallback_release": overture.OVERTURE_FALLBACK_RELEASE,
        "s3_base": overture.OVERTURE_S3_BASE,
    }

    # 1) 列举可用版本
    t0 = time.time()
    try:
        releases = overture.list_releases(proxy)
        out["list_ok"] = True
        out["release_count"] = len(releases)
        out["releases_tail"] = releases[-8:]      # 只回最近几个,避免刷屏
        out["list_ms"] = int((time.time() - t0) * 1000)
    except Exception as e:
        out["list_ok"] = False
        out["releases_tail"] = []
        out["list_error"] = str(e)[:400]
        out["list_ms"] = int((time.time() - t0) * 1000)

    # 2) 探测将实际使用的版本能否读到数据
    target = configured or (out.get("releases_tail") or [None])[-1] \
        or overture.OVERTURE_FALLBACK_RELEASE
    out["probe_release"] = target
    t1 = time.time()
    try:
        out["probe_ok"] = overture.probe_release(target, proxy)
    except Exception as e:
        out["probe_ok"] = False
        out["probe_error"] = str(e)[:400]
    out["probe_ms"] = int((time.time() - t1) * 1000)

    # 3) 给出处置建议
    if out.get("probe_ok"):
        out["verdict"] = f"可用。将使用版本 {target}。"
    elif out.get("list_ok") and out.get("release_count"):
        out["verdict"] = (
            f"能列举到 {out['release_count']} 个版本,但 {target} 读不到数据。"
            "请在 config.yaml 的 buildings.release 指定 releases_tail 中的某个版本。"
        )
    else:
        out["verdict"] = (
            "无法访问 Overture S3。若在境内,请在 config.yaml 配置 "
            "buildings.proxy(如 127.0.0.1:7890)后重启后端;"
            "若已配代理仍失败,请确认代理支持 HTTPS CONNECT 到 "
            "overturemaps-us-west-2.s3.us-west-2.amazonaws.com。"
        )
    return out


@router.get("/diagnose")
async def api_diagnose():
    """诊断 Overture 数据源可达性。耗时可能十几秒(联网探测),故放线程池。"""
    return await asyncio.to_thread(_diagnose)


@router.post("/upload")
async def api_upload_vector(payload: dict = Body(...)):
    """接收前端已转好的 WGS84 GeoJSON,存盘并返回字段统计。

    前端负责格式解析与坐标转换(shpjs + proj4,能处理 .prj 与 CGCS2000 带号),
    后端只存盘 + 探测字段,不引入 fiona/pyshp。

    返回 {upload_id, feature_count, polygon_count, bbox, fields:[...]},
    前端据 fields 的填充率/数值率让用户挑高度字段与要保留的字段。
    """
    if not isinstance(payload, dict) or not payload.get("type"):
        raise HTTPException(400, "请求体必须是 GeoJSON 对象")
    try:
        return await asyncio.to_thread(vector_upload.save_upload, payload)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"保存上传数据失败:{str(e)[:200]}") from e


@router.get("/upload/{upload_id}/fields")
async def api_upload_fields(upload_id: str):
    """重新读取某次上传的字段统计(重新下载弹窗回填字段选项时用)。"""
    try:
        gj = await asyncio.to_thread(vector_upload.load_upload, upload_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    info = await asyncio.to_thread(vector_upload.analyze, gj)
    return {"upload_id": upload_id, **info}


@router.post("/dem/upload")
async def api_upload_dem(request: Request, filename: str = ""):
    """上传本地地形 GeoTIFF,用于建筑底面高采样(优先于在线地形)。

    请求体是文件原始字节(Content-Type 任意),文件名用 ?filename= 传。
    没用 multipart:那需要额外装 python-multipart,而这里用原始请求流能
    **边收边写盘**,几百 MB 的地形不必整份进内存。

    返回 {dem_id, crs, width, height, bounds_wgs84, res_m_approx, ...}。
    前端据 bounds_wgs84 与下载范围比对,覆盖不全时提示——未覆盖区域会回落
    在线地形兜底,不会丢建筑,但两种高程源基准可能不同、交界处可能有台阶。
    """
    from ..core import dem_upload

    dem_id, tmp = await asyncio.to_thread(dem_upload.new_temp)
    written = 0
    try:
        with tmp.open("wb") as f:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > dem_upload.MAX_BYTES:
                    raise HTTPException(400, "文件过大,上限 4 GB")
                f.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise HTTPException(500, f"接收地形失败:{str(e)[:200]}") from e

    try:
        return await asyncio.to_thread(dem_upload.commit, dem_id, tmp, filename)
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    except Exception as e:
        raise HTTPException(500, f"解析地形失败:{str(e)[:300]}") from e


@router.get("/dem/{dem_id}")
async def api_dem_info(dem_id: str):
    """已上传地形的信息(重新下载弹窗回填时用)。"""
    from ..core import dem_upload

    meta = await asyncio.to_thread(dem_upload.meta_of, dem_id)
    if not meta:
        raise HTTPException(404, "地形数据不存在或已过期")
    return meta


@router.get("/cache")
async def api_cache_stats():
    """建筑轮廓网格缓存的占用情况(按数据源分)。"""
    def _run():
        from ..core.building_cache import CELL_DEG, BuildingCache, cache_root
        ttl = float(getattr(getattr(settings, "buildings", None), "cache_ttl_days", 30.0))
        items = []
        root = cache_root()
        for d in sorted(p for p in root.iterdir() if p.is_dir()):
            items.append(BuildingCache(d.name, ttl_days=ttl).stats())
        return {
            "cell_deg": CELL_DEG,
            "ttl_days": ttl,
            "root": str(root),
            "items": items,
            "total_bytes": sum(i["bytes"] for i in items),
            "total_cells": sum(i["cells"] for i in items),
        }

    return await asyncio.to_thread(_run)


@router.delete("/cache")
async def api_cache_clear(source: str = "osm", west: float | None = None,
                         south: float | None = None, east: float | None = None,
                         north: float | None = None):
    """清理建筑缓存。给全套 bbox 则只清该范围覆盖的格子,否则清空该数据源。"""
    bbox = None
    if None not in (west, south, east, north):
        if east <= west or north <= south:
            raise HTTPException(400, "范围无效")
        bbox = (west, south, east, north)

    def _run():
        from ..core.building_cache import BuildingCache
        ttl = float(getattr(getattr(settings, "buildings", None), "cache_ttl_days", 30.0))
        return BuildingCache(source, ttl_days=ttl).clear(bbox=bbox)

    cleared = await asyncio.to_thread(_run)
    return {"source": source, "cleared_cells": cleared,
            "scope": "bbox" if bbox else "all"}


# 说明:pbf 导入与打包属于"准备数据"的运维动作,只在准备机上跑一次
# (全国约半小时),故不提供 HTTP 接口、界面也不暴露,统一由
# update-building-data.bat 手动执行。使用机器只需配好 buildings.remote_url。


@router.get("/bundles")
async def api_bundles_status(source: str = "osm"):
    """远端数据包配置与本地打包产物状态。"""
    def _run():
        from ..core.bundle_pack import dist_root
        from ..core.remote_bundles import RemoteBundleStore
        d = dist_root() / source
        local = {"exists": d.is_dir(), "dir": str(d)}
        if d.is_dir():
            packs = list(d.glob("b_*.json.gz"))
            local.update(bundles=len(packs),
                         bytes=sum(p.stat().st_size for p in packs),
                         has_index=(d / "index.json").exists())
        store = RemoteBundleStore(source)
        remote = {"enabled": store.enabled, "url": store.base}
        if store.enabled:
            idx = store.index()
            if idx:
                remote.update(version=idx.get("version"),
                              bundle_count=idx.get("bundle_count"),
                              total_bytes=idx.get("total_bytes"),
                              coverage_bbox=idx.get("coverage_bbox"))
            else:
                remote["error"] = "清单不可读(地址错误或网络不可达)"
        return {"source": source, "local_dist": local, "remote": remote}

    return await asyncio.to_thread(_run)


@router.get("/count")
async def api_count(west: float, south: float, east: float, north: float):
    """查询某范围内的 Overture 建筑数,用于验证 bbox 谓词下推是否真的生效。"""
    if east <= west or north <= south:
        raise HTTPException(400, "范围无效")

    def _run() -> dict:
        src = overture.build_building_source(
            "overture_buildings", release=_configured_release(), proxy=_proxy())
        t0 = time.time()
        n = src.count((west, south, east, north))
        return {
            "release": src.release,
            "bbox": [west, south, east, north],
            "count": n,
            "elapsed_ms": int((time.time() - t0) * 1000),
        }

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        raise HTTPException(502, f"查询失败:{str(e)[:400]}") from e
