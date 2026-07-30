"""任务数据模型与持久化操作。"""
from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

from .config import settings
from .db import get_conn


# ---------- 导出格式解析 ----------

def parse_export(value: str) -> list[str]:
    """把导出格式字段解析成格式列表,兼容旧值 both/geotiff+tms。

    影像格式:geotiff/tms/osm;DEM 格式:geotiff/tiles/terrain/hillshade。
    白名单覆盖两条管线全部格式,非法值过滤后回退 geotiff。
    """
    v = (value or "geotiff").strip().lower()
    if v == "both":
        return ["geotiff", "tms"]
    parts = [p.strip() for p in v.replace("+", ",").split(",") if p.strip()]
    allowed = ("geotiff", "tms", "osm", "tiles", "terrain", "hillshade", "b3dm")
    out = [p for p in parts if p in allowed]
    return out or ["geotiff"]


# ---------- 阶段化进度定义 ----------

# 阶段标签(展示名);DEM 的高程/晕渲阶段标签按勾选动态生成
STAGE_LABELS = {
    "download": "下载原始瓦片",
    "geotiff": "合并 GeoTIFF",
    "tms": "切 TMS 瓦片",
    "osm": "切 OSM 瓦片",
    "dem": "高程 GeoTIFF",
    "tiles": "导出原始瓦片",
    "terrain": "切 Cesium 地形",
    # 三维建筑白模管线
    "fetch_buildings": "拉取建筑轮廓",
    "base_dem": "准备底面高程",
    "build_mesh": "建筑白模建模",
    "tile_3d": "切 3D Tiles(b3dm)",
}


def _new_stage(key: str, label: str) -> dict:
    """初始化一个 pending 阶段记录。"""
    return {
        "key": key, "label": label, "status": "pending",
        "percent": 0.0, "done": 0, "total": 0, "eta_sec": None, "message": "",
    }


def build_stage_defs(provider: str, formats: list[str], annotate: bool = False,
                     base_height_mode: str = "terrain") -> list[dict]:
    """按数据源与导出格式生成有序阶段列表(仅含实际会执行的阶段)。

    影像管线:download → geotiff → tms → osm
    DEM 管线:download → dem(高程/晕渲) → tiles → terrain
    三维建筑:fetch_buildings → base_dem(仅 terrain 模式) → build_mesh → tile_3d
    """
    from .providers.buildings import is_building_provider
    from .providers.terrain import is_dem_provider

    # 三维建筑管线不下载栅格瓦片,阶段构成完全不同
    if is_building_provider(provider):
        stages = [_new_stage("fetch_buildings", STAGE_LABELS["fetch_buildings"])]
        if base_height_mode == "terrain":
            stages.append(_new_stage("base_dem", STAGE_LABELS["base_dem"]))
        stages.append(_new_stage("build_mesh", STAGE_LABELS["build_mesh"]))
        stages.append(_new_stage("tile_3d", STAGE_LABELS["tile_3d"]))
        return stages

    stages: list[dict] = [_new_stage("download", STAGE_LABELS["download"])]
    if is_dem_provider(provider):
        want_geotiff = "geotiff" in formats
        want_hillshade = "hillshade" in formats
        if want_geotiff or want_hillshade:
            if want_geotiff and want_hillshade:
                label = "高程 + 晕渲 GeoTIFF"
            elif want_hillshade:
                label = "晕渲图"
            else:
                label = "高程 GeoTIFF"
            stages.append(_new_stage("dem", label))
        if "tiles" in formats:
            stages.append(_new_stage("tiles", STAGE_LABELS["tiles"]))
        if "terrain" in formats:
            stages.append(_new_stage("terrain", STAGE_LABELS["terrain"]))
    else:
        if "geotiff" in formats:
            stages.append(_new_stage("geotiff", STAGE_LABELS["geotiff"]))
        if "tms" in formats:
            stages.append(_new_stage("tms", STAGE_LABELS["tms"]))
        if "osm" in formats:
            stages.append(_new_stage("osm", STAGE_LABELS["osm"]))
    return stages


# ---------- 输出目录命名 ----------

_INVALID = re.compile(r'[\\/:*?"<>|]')  # Windows 非法文件名字符


def safe_dirname(name: str) -> str:
    """把任务名清理成合法目录名;为空则回落 'task'。"""
    cleaned = _INVALID.sub("_", (name or "").strip()).strip(". ")
    return cleaned or "task"


def reserve_output_dir(name: str) -> Path:
    """按任务名生成唯一输出目录并立即创建(占位),重名则追加 _1、_2…。"""
    base = settings.output_dir
    base.mkdir(parents=True, exist_ok=True)
    stem = safe_dirname(name)
    candidate = base / stem
    i = 1
    while candidate.exists():
        candidate = base / f"{stem}_{i}"
        i += 1
    candidate.mkdir(parents=True)
    return candidate


# ---------- 请求/响应模型(API 层用) ----------

class HillshadeParams(BaseModel):
    """DEM 晕渲光照参数。"""
    azimuth: float = Field(default=315.0, ge=0.0, le=360.0, description="光源方位角(度,0=北,315=西北)")
    altitude: float = Field(default=45.0, ge=0.0, le=90.0, description="光源高度角(度)")
    z_factor: float = Field(default=1.0, gt=0.0, le=10.0, description="垂直夸张系数")


class TaskCreate(BaseModel):
    name: str = Field(default="未命名任务")
    provider: str = Field(default="tianditu_img")
    bbox: list[float] = Field(..., description="[west, south, east, north] 经纬度")
    # 选中的级别列表(勾选模式)。为兼容旧客户端仍接受 z_min/z_max。
    levels: list[int] = Field(default_factory=list, description="选中的级别,如 [10,11,12]")
    z_min: int = Field(default=0, ge=0, le=18)
    z_max: int = Field(default=0, ge=0, le=18)
    export: str = Field(default="geotiff", description="导出格式,逗号分隔:geotiff/tms/osm")
    geometry: Optional[dict] = Field(default=None, description="geojson 几何(矢量/多边形),WGS84")
    clip: bool = Field(default=False, description="是否裁剪 GeoTIFF 到 geometry 边界")
    crs: str = Field(default="EPSG:4326", description="GeoTIFF 输出坐标系")
    use_cache: bool = Field(default=True, description="是否复用瓦片缓存(False=强制重下原始瓦片)")
    annotate: bool = Field(default=False, description="是否叠加路网注记(同步下载注记图层并烘焙进成果)")
    hillshade: HillshadeParams = Field(default_factory=HillshadeParams, description="DEM 晕渲光照参数")
    # ---- 三维建筑(Overture → b3dm)参数 ----
    base_height_mode: str = Field(
        default="terrain",
        description="建筑底面高模式:terrain(采样DEM,推荐)/flat(固定0)/offset(统一偏移)")
    height_offset: float = Field(
        default=0.0, ge=-1000.0, le=10000.0,
        description="底面高附加偏移(米);offset 模式下即为底面高")
    default_height: float = Field(
        default=6.0, gt=0.0, le=1000.0, description="建筑高度全缺失时的兜底高(米)")
    max_per_tile: int = Field(
        default=2000, ge=100, le=50000, description="单个 b3dm 瓦片建筑数上限")
    # ---- 本地矢量面上传(local_vector)的字段映射 ----
    upload_id: str = Field(default="", description="上传的矢量数据 id")
    height_field: str = Field(default="", description="取高度的属性字段名")
    height_mode: str = Field(
        default="none", description="高度字段含义:meters(米)/floors(层数)/none(不用字段)")
    height_scale: float = Field(
        default=1.0, gt=0.0, le=1000.0, description="字段值换算系数(厘米→米填 0.01)")
    floor_height: float = Field(
        default=3.0, gt=0.0, le=100.0, description="floors 模式的单层层高(米)")
    name_field: str = Field(default="", description="作为建筑名的字段")
    keep_fields: list[str] = Field(
        default_factory=list, description="写入 b3dm Batch Table 的属性字段名")
    dem_upload_id: str = Field(
        default="",
        description="上传的地形 GeoTIFF id;优先用于底面高采样,未覆盖处回落在线地形")

    def level_list(self) -> list[int]:
        """归一化出去重升序的级别列表:优先 levels,回退 z_min..z_max。

        天地图级别 1-18;DEM(Esri Terrain3D)级别 0-16(允许 0 级)。
        """
        from .providers.buildings import is_building_provider
        from .providers.terrain import DEM_LAYERS, is_dem_provider
        # 三维建筑无瓦片级别概念(数据是矢量要素集);返回合成级别让通用校验通过,
        # 底面高程 DEM 的级别由 runner 按范围自行决定。
        if is_building_provider(self.provider):
            return [0]
        if is_dem_provider(self.provider):
            z_floor, z_cap = 0, DEM_LAYERS[self.provider][2]
        else:
            z_floor, z_cap = 1, 18
        if self.levels:
            return sorted({z for z in self.levels if z_floor <= z <= z_cap})
        if self.z_min is not None and self.z_max is not None and self.z_min <= self.z_max:
            return list(range(max(self.z_min, z_floor), min(self.z_max, z_cap) + 1))
        return []


# ---------- 持久化辅助 ----------

def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def create_task(data: TaskCreate, total: int, est_bytes: int = 0) -> str:
    task_id = uuid.uuid4().hex[:12]
    now = _now()
    # 提交时即按任务名锁定唯一输出目录,避免随机字符串
    out_dir = reserve_output_dir(data.name)
    levels = data.level_list()
    # z_min/z_max 从选中级别派生,兼容旧的列显示与查询
    z_min, z_max = (levels[0], levels[-1]) if levels else (data.z_min, data.z_max)
    # 初始化阶段化进度(下载 + 各勾选导出阶段)
    stages = build_stage_defs(data.provider, parse_export(data.export), data.annotate,
                              data.base_height_mode)
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tasks
               (id, name, provider, bbox, z_min, z_max, export,
                status, total, downloaded, failed, message,
                output_path, geometry, clip, crs, levels, use_cache, annotate,
                hillshade, stages, est_bytes,
                base_height_mode, height_offset, default_height, max_per_tile,
                building_count,
                upload_id, height_field, height_mode, height_scale,
                floor_height, name_field, keep_fields, dem_upload_id,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,
                       ?,?,?,?,?,?,?,?,?,?)""",
            (
                task_id, data.name, data.provider, json.dumps(data.bbox),
                z_min, z_max, data.export,
                "pending", total, 0, 0, "", str(out_dir),
                json.dumps(data.geometry) if data.geometry else "",
                1 if data.clip else 0, data.crs or "EPSG:4326",
                json.dumps(levels), 1 if data.use_cache else 0,
                1 if data.annotate else 0,
                json.dumps(data.hillshade.model_dump()),
                json.dumps(stages), int(est_bytes or 0),
                data.base_height_mode, float(data.height_offset),
                float(data.default_height), int(data.max_per_tile), 0,
                data.upload_id or "", data.height_field or "",
                data.height_mode or "none", float(data.height_scale),
                float(data.floor_height), data.name_field or "",
                json.dumps(data.keep_fields or [], ensure_ascii=False),
                data.dem_upload_id or "",
                now, now,
            ),
        )
    return task_id


def update_task(task_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = _now()
    cols = ", ".join(f"{k}=?" for k in fields)
    with get_conn() as conn:
        conn.execute(
            f"UPDATE tasks SET {cols} WHERE id=?",
            (*fields.values(), task_id),
        )


def get_task(task_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    return _row_to_dict(row) if row else None


def delete_task(task_id: str) -> None:
    with get_conn() as conn:
        conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))


def list_tasks() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC"
        ).fetchall()
    return [_row_to_dict(r) for r in rows]


def reset_stale_running() -> None:
    """服务重启时,把残留的 running/pending 任务标记为 paused(可续传),避免僵死。

    paused 状态本身保留,用户可点「开始」用瓦片缓存断点续传。
    """
    with get_conn() as conn:
        conn.execute(
            "UPDATE tasks SET status='paused', message='服务重启,已暂停(可继续)' "
            "WHERE status IN ('running','pending')"
        )


def _row_to_dict(row) -> dict:
    d = dict(row)
    d["bbox"] = json.loads(d["bbox"])
    # 兼容旧库缺列
    geom = d.get("geometry")
    d["geometry"] = json.loads(geom) if geom else None
    d["clip"] = bool(d.get("clip", 0))
    d["crs"] = d.get("crs") or "EPSG:4326"
    d["use_cache"] = bool(d.get("use_cache", 1))
    d["annotate"] = bool(d.get("annotate", 0))
    hs = d.get("hillshade")
    d["hillshade"] = json.loads(hs) if hs else {"azimuth": 315.0, "altitude": 45.0, "z_factor": 1.0}
    # 级别列表:优先存储的 levels,旧任务(空)回退连续区间
    lv = d.get("levels")
    parsed = json.loads(lv) if lv else []
    d["levels"] = parsed if parsed else list(range(d["z_min"], d["z_max"] + 1))
    if d["total"]:
        d["progress"] = round(d["downloaded"] / d["total"] * 100, 1)
    else:
        d["progress"] = 0.0
    # 预估原始瓦片下载量(字节);旧任务缺列为 0
    d["est_bytes"] = d.get("est_bytes") or 0
    # 三维建筑参数;旧任务缺列时回落默认值
    d["base_height_mode"] = d.get("base_height_mode") or "terrain"
    d["height_offset"] = float(d.get("height_offset") or 0.0)
    d["default_height"] = float(d.get("default_height") or 6.0)
    d["max_per_tile"] = int(d.get("max_per_tile") or 2000)
    d["building_count"] = int(d.get("building_count") or 0)
    # 本地矢量面字段映射;旧任务缺列时回落默认
    d["upload_id"] = d.get("upload_id") or ""
    d["height_field"] = d.get("height_field") or ""
    d["height_mode"] = d.get("height_mode") or "none"
    d["height_scale"] = float(d.get("height_scale") or 1.0)
    d["floor_height"] = float(d.get("floor_height") or 3.0)
    d["name_field"] = d.get("name_field") or ""
    kf = d.get("keep_fields")
    try:
        d["keep_fields"] = json.loads(kf) if kf else []
    except (TypeError, ValueError):
        d["keep_fields"] = []
    d["dem_upload_id"] = d.get("dem_upload_id") or ""
    # 阶段化进度:优先存储的 stages;旧任务(空)按 export/status 合成兼容视图
    st = d.get("stages")
    stages = json.loads(st) if st else []
    if not stages:
        stages = _synth_stages(d)
    d["stages"] = stages
    return d


def _synth_stages(d: dict) -> list[dict]:
    """为无 stages 字段的旧任务合成阶段视图(仅展示用,不落库)。

    按任务当前状态推断:done 则全部标 done;running/其他按下载进度体现在
    download 阶段,导出阶段维持 pending。
    """
    stages = build_stage_defs(d["provider"], parse_export(d.get("export", "geotiff")),
                              bool(d.get("annotate")),
                              d.get("base_height_mode") or "terrain")
    status = d.get("status")
    dl_total = d.get("total", 0)
    dl_done = d.get("downloaded", 0)
    for s in stages:
        if status == "done":
            s["status"] = "done"
            s["percent"] = 100.0
        elif s["key"] == "download":
            s["total"] = dl_total
            s["done"] = dl_done
            s["percent"] = round(dl_done / dl_total * 100, 1) if dl_total else 0.0
            if status == "running":
                s["status"] = "running"
            elif status in ("paused", "failed", "canceled"):
                s["status"] = "paused" if status == "paused" else status
    return stages


def update_stages(task_id: str, stages: list[dict]) -> None:
    """把阶段进度数组落库(整体覆盖写)。"""
    update_task(task_id, stages=json.dumps(stages))
