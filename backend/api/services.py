"""本地数据服务的 HTTP 端点。

**安全边界（刻意与 api/local.py 不同）**：本模块的文件服务端点**不检查请求
来源**。这是为了"给本机其他服务当数据源用"——那些服务不是浏览器，走
_require_local 会被直接挡掉。

由此付出的代价：若把 config.yaml 的 server.host 从 127.0.0.1 改成 0.0.0.0，
知道 URL 的局域网设备就能读到已开启的服务内容。默认 127.0.0.1 下暴露面仅本机。

补偿措施：
  - 每个服务的 URL 含 8 位随机 id，不可枚举
  - 新增服务默认关闭，必须显式开启
  - 路径穿越在每个请求上校验（service_registry.check_path）

**错误语义**（前端据此区分，不要合并）：
  403  服务未开启 / 路径越界
  404  服务不存在 / 文件不存在
  410  服务存在、但源目录或入口文件已失效
"""
from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from ..core import service_registry as reg
from ..core import service_scan
from ..core.logs import logger

router = APIRouter(tags=["services"])

#: 瓦片类服务拼扩展名的兜底
_DEFAULT_EXT = "png"

#: 扩展名 -> Content-Type。3D Tiles 的二进制块要正确声明，
#: 否则 Cesium 会把 .pnts 当文本处理。
_MIME = {
    ".json": "application/json",
    ".geojson": "application/geo+json",
    ".kml": "application/vnd.google-earth.kml+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".terrain": "application/vnd.quantized-mesh",
    ".b3dm": "application/octet-stream",
    ".pnts": "application/octet-stream",
    ".i3dm": "application/octet-stream",
    ".cmpt": "application/octet-stream",
    ".glb": "model/gltf-binary",
    ".xml": "application/xml",
}


def _access_path(svc: dict) -> str:
    """服务的访问路径（不含主机名）——前端拼 location.origin。

    主机名由前端补：后端只知道 server.host（可能是 127.0.0.1），而实际访问者
    可能是局域网 IP 或域名。

    瓦片模板里的 {z}{x}{y} **原样输出**，不做 URL 编码——转义花括号会让
    前端的 URL 模板失效。
    """
    base = f"/api/svc/{svc['id']}"
    if svc["kind"] == service_scan.KIND_IMAGERY and svc.get("grid"):
        ext = svc.get("tile_ext") or _DEFAULT_EXT
        return f"{base}/{{z}}/{{x}}/{{y}}.{ext}"
    if svc["kind"] == service_scan.KIND_TERRAIN:
        return base           # Cesium 会自行追加 /layer.json
    entry = svc.get("entry") or ""
    return f"{base}/{entry}" if entry else base


def _overlay_desc(svc: dict) -> dict:
    """转成前端 overlays.js::build() 认识的 desc。

    结构与 core/overlay.py::list_layers() 的输出逐字段对齐，使服务图层零改动
    复用既有的透明度/层级/定位能力。

    **每种 kind 都必须带 bounds_wgs84**：现有 list_layers 里 6 种 kind 有 5 种
    不带（靠任务 bbox 兜底），而服务图层没有任务，不补就永远无法定位。

    模型/地形在二维地图上只能画范围框，故 kind 复用既有的 'raster_only_bbox'
    ——不引入新 kind，从而不改 build() 与两份 KIND_TEXT 映射表。
    """
    path = _access_path(svc)
    kind = svc["kind"]
    if kind == service_scan.KIND_IMAGERY and svc.get("grid"):
        return {
            "id": svc["id"], "kind": "tiles", "label": svc["name"],
            "url": path,
            "grid": svc["grid"], "flip_y": bool(svc.get("flip_y")),
            "minzoom": int(svc.get("minzoom") or 0),
            "maxzoom": int(svc.get("maxzoom") or 18),
            "bounds_wgs84": svc.get("bounds_wgs84"),
            # 关掉的服务在二维地图上选中会立刻 403，前端据此提示
            "enabled": bool(svc.get("enabled")),
        }
    if kind == service_scan.KIND_VECTOR:
        return {
            "id": svc["id"], "kind": "vector", "label": svc["name"],
            "url": path, "bounds_wgs84": svc.get("bounds_wgs84"),
            "enabled": bool(svc.get("enabled")),
        }
    return {
        "id": svc["id"], "kind": "raster_only_bbox", "label": svc["name"],
        "url": path, "bounds_wgs84": svc.get("bounds_wgs84"),
        "enabled": bool(svc.get("enabled")),
    }


def _to_json(svc: dict) -> dict:
    d = dict(svc)
    d["access_path"] = _access_path(svc)
    d["overlay_desc"] = _overlay_desc(svc)
    return d


def _candidate_json(c: service_scan.Candidate) -> dict:
    return {
        "kind": c.kind, "root": c.root, "entry": c.entry, "label": c.label,
        "grid": c.grid, "flip_y": c.flip_y,
        "minzoom": c.minzoom, "maxzoom": c.maxzoom,
        "bounds_wgs84": c.bounds_wgs84, "bounds_approx": c.bounds_approx,
        "tile_ext": c.tile_ext,
    }


# ---------- 列表与探活 ----------

@router.get("/api/services")
async def api_list_services():
    return {"services": [_to_json(s) for s in reg.list_services()]}


@router.get("/api/services/health")
async def api_services_health():
    return {"health": reg.health_all()}


# ---------- 扫描与候选 ----------

class ScanReq(BaseModel):
    path: str = Field(..., description="要扫描的本地目录绝对路径")


@router.post("/api/services/scan")
async def api_scan(data: ScanReq):
    """扫描任意本地目录，返回可发布的成果候选（不落库）。"""
    p = Path((data.path or "").strip().strip('"'))
    if not p.is_absolute():
        raise HTTPException(400, "请提供绝对路径")
    if not p.is_dir():
        raise HTTPException(404, f"目录不存在：{p}")
    cands = await asyncio.to_thread(service_scan.scan_dir, p)
    return {"candidates": [_candidate_json(c) for c in cands]}


@router.get("/api/services/candidates")
async def api_output_candidates():
    """扫 output 目录得到的候选（面板打开时调）。

    **扫文件系统而非读数据库**：实测库中 106 条 output_path 记录有 100 条目录
    已不存在（成果被清理但记录留存），读库会生成 100 个死服务。
    """
    from ..config import settings

    root = settings.output_dir
    if not root.is_dir():
        return {"candidates": []}
    cands = await asyncio.to_thread(service_scan.scan_dir, root)
    existing = {(s["kind"], s["root"], s["entry"]) for s in reg.list_services()}
    out = [
        _candidate_json(c) for c in cands
        if (c.kind, c.root, c.entry) not in existing
    ]
    return {"candidates": out}


# ---------- 注册与修改 ----------

class RegisterReq(BaseModel):
    name: str = Field(default="", description="显示名，默认取路径尾段")
    kind: str = Field(..., description="model / imagery / vector / terrain")
    root: str = Field(..., description="服务根目录绝对路径")
    entry: str = Field(default="")
    grid: str = Field(default="")
    flip_y: bool = Field(default=False)
    minzoom: int = Field(default=0)
    maxzoom: int = Field(default=18)
    bounds_wgs84: list[float] | None = Field(default=None)
    bounds_approx: bool = Field(default=False)
    tile_ext: str = Field(default="png")
    source: str = Field(default="manual")


@router.post("/api/services")
async def api_register(data: RegisterReq):
    root = Path((data.root or "").strip().strip('"'))
    if not root.is_absolute():
        raise HTTPException(400, "请提供绝对路径")
    if not root.is_dir():
        raise HTTPException(404, f"目录不存在：{root}")
    if data.kind not in service_scan.KIND_LABELS:
        raise HTTPException(
            400, f"不支持的类型：{data.kind}（可选：" +
                 "、".join(service_scan.KIND_LABELS) + "）")
    name = (data.name or "").strip() or root.name
    sid = reg.add_service(
        name=name, kind=data.kind, root=str(root), entry=data.entry,
        grid=data.grid, flip_y=data.flip_y, minzoom=data.minzoom,
        maxzoom=data.maxzoom, bounds_wgs84=data.bounds_wgs84,
        bounds_approx=data.bounds_approx, tile_ext=data.tile_ext,
        source=data.source)
    return _to_json(reg.get_service(sid))


class PatchReq(BaseModel):
    name: str | None = None
    enabled: bool | None = None


@router.patch("/api/services/{sid}")
async def api_patch(sid: str, data: PatchReq):
    if reg.get_service(sid) is None:
        raise HTTPException(404, "服务不存在")
    fields = {k: v for k, v in data.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "没有要修改的字段")
    reg.update_service(sid, **fields)
    return _to_json(reg.get_service(sid))


@router.delete("/api/services/{sid}")
async def api_remove(sid: str):
    """移除注册记录。**不删磁盘文件**——服务是"指向"，不是"拥有"。"""
    if not reg.remove_service(sid):
        raise HTTPException(404, "服务不存在")
    return {"removed": sid}


# ---------- 文件服务 ----------

def _serve_file(svc: dict, sub: str):
    """按注册根服务一个文件。所有错误都带明确文案，前端据此区分状态。

    **sub 是相对 root 的路径，entry 不参与拼接。** root 在登记时就已经是
    "能作为服务根的那个目录"（模型/地形是 tileset.json 与 layer.json 所在的
    目录）。entry 只标识"入口文件是哪个"，用于根路由与健康探测。

    这一点容易写错：3D Tiles 的 tileset.json 内部用相对**自己所在目录**的
    URI 引用资源（如 ./Data/_pyramid/r.b3dm），若把 entry 当目录前缀拼在
    sub 前，会得到 tileset.json/Data/_pyramid/r.b3dm 这种不存在的路径，
    所有子资源 404，三维瓦片整片加载不出来。
    """
    health = reg.service_health(svc["id"])
    if not health["ok"]:
        # 410 Gone：源曾经存在、现在没了。与 403（未开启）区分开——
        # 开关状态和路径写错是两回事，报错必须能分辨。
        raise HTTPException(410, f"源已失效：{health['reason']}")
    if not svc["enabled"]:
        raise HTTPException(403, "服务未开启")

    root = Path(svc["root"])
    try:
        target = reg.check_path(root, sub)
    except PermissionError:
        logger.warning("服务 %s 拒绝越界路径：%s", svc["id"], sub)
        raise HTTPException(403, "路径越界")
    if target.is_dir():
        raise HTTPException(404, "这里是目录，不是文件")
    if not target.is_file():
        raise HTTPException(404, f"文件不存在：{sub}")
    # FileResponse 自带 Range 支持（返回 206）——这是 ol/source/GeoTIFF
    # 按需读块的前提
    return FileResponse(str(target), media_type=_MIME.get(target.suffix.lower()))


@router.get("/api/svc/{sid}")
async def api_svc_root(sid: str):
    """服务根：返回该服务的入口文件。

    模型/矢量有 entry（tileset.json / 文件名），这里直接返回它；
    地形与瓦片目录没有单一入口（Cesium 会自行追加 /layer.json 或
    /{z}/{x}/{y}）——那种情况下给一句明确的说明，而不是让前端拿到
    一个含义不明的 404。
    """
    svc = reg.get_service(sid)
    if svc is None:
        raise HTTPException(404, "服务不存在")
    entry = svc.get("entry") or ""
    if not entry:
        kind = svc["kind"]
        if kind == service_scan.KIND_TERRAIN:
            raise HTTPException(
                404, "地形服务没有根级文件，请访问 /layer.json 或其下的瓦片路径")
        raise HTTPException(
            404, "瓦片服务没有根级文件，请按 /{z}/{x}/{y} 模板访问")
    return _serve_file(svc, entry)


@router.get("/api/svc/{sid}/{sub:path}")
async def api_svc(sid: str, sub: str):
    svc = reg.get_service(sid)
    if svc is None:
        raise HTTPException(404, "服务不存在")
    return _serve_file(svc, sub)
