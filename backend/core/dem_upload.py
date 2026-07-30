"""上传的地形 GeoTIFF 的存储与校验(供建筑底面高采样)。

用途:用户手上常有比在线 DEM 更精细的本地地形(实测、机载 LiDAR、院内成果),
用它算建筑底面高比在线 30m 级数据准得多。

范围一致性:上传的地形**应当覆盖**下载范围(或上传矢量面的范围)。若只覆盖了
一部分,不完全一致的区域会回落到在线地形兜底(见 core/buildings.MultiDemSampler),
不会因此丢建筑;但两种高程源基准可能不同,交界处可能出现台阶,故界面会提示。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path

from ..config import settings
from .logs import logger

#: 上传文件保留天数(超期在下次上传时清理)
UPLOAD_TTL_DAYS = 30
#: 单个文件大小上限(字节)。地形动辄很大,给到 4GB(BIGTIFF)
MAX_BYTES = 4 * 1024 ** 3


def uploads_dir() -> Path:
    d = settings.abs_path("./data/uploads/dem")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _path_of(dem_id: str) -> Path:
    if not re.fullmatch(r"[0-9a-f]{8,32}", dem_id or ""):
        raise ValueError("非法的地形 id")
    return uploads_dir() / f"{dem_id}.tif"


def _meta_path(dem_id: str) -> Path:
    return _path_of(dem_id).with_suffix(".json")


def cleanup_old() -> int:
    cutoff = time.time() - UPLOAD_TTL_DAYS * 86400
    n = 0
    for p in uploads_dir().glob("*.tif"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink(missing_ok=True)
                p.with_suffix(".json").unlink(missing_ok=True)
                n += 1
        except OSError:
            pass
    if n:
        logger.info("清理过期上传地形 %d 个", n)
    return n


def inspect(path: Path) -> dict:
    """读取地形基本信息,并把范围换算到 WGS84(供与下载范围比对)。

    要求:单波段以上的栅格、必须带 CRS(没有 CRS 无法定位,采样会全落空)。
    """
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as ds:
        if not ds.crs:
            raise ValueError(
                "该 GeoTIFF 没有坐标系信息(CRS),无法定位到地理位置。"
                "请用 GIS 软件赋予正确坐标系后重新上传。")
        b = ds.bounds
        try:
            w, s, e, n = transform_bounds(ds.crs, "EPSG:4326",
                                          b.left, b.bottom, b.right, b.top,
                                          densify_pts=21)
        except Exception as ex:
            raise ValueError(f"坐标系无法转换到 WGS84:{ex}") from ex
        # 分辨率换算成米的粗略值(仅用于展示)
        res_x = abs(ds.transform.a)
        unit_m = 1.0
        if str(ds.crs).upper().endswith("4326"):
            unit_m = 111320.0
        return {
            "crs": str(ds.crs),
            "width": ds.width,
            "height": ds.height,
            "bands": ds.count,
            "dtype": ds.dtypes[0],
            "nodata": (None if ds.nodata is None else float(ds.nodata)),
            "bounds_wgs84": [w, s, e, n],
            "res_native": res_x,
            "res_m_approx": round(res_x * unit_m, 3),
        }


def new_temp() -> tuple[str, Path]:
    """分配一个上传 id 与临时落盘路径(供流式写入)。

    地形文件动辄几百 MB,不能整份读进内存再落盘,故由调用方按块写入这个
    临时文件,写完再调 commit 校验并转正。
    """
    cleanup_old()
    dem_id = uuid.uuid4().hex[:12]
    return dem_id, _path_of(dem_id).with_suffix(".tif.part")


def commit(dem_id: str, tmp: Path, filename: str = "") -> dict:
    """校验流式写好的临时文件并转为正式副本,返回信息 dict。

    校验不过就删掉临时文件——宁可让用户重传,也不留一个采样时才发现
    没有坐标系的"半可用"文件。
    """
    path = _path_of(dem_id)
    if not (tmp.exists() and tmp.stat().st_size > 0):
        tmp.unlink(missing_ok=True)
        raise ValueError("上传内容为空")
    size = tmp.stat().st_size
    if size > MAX_BYTES:
        tmp.unlink(missing_ok=True)
        raise ValueError(f"文件过大({size / 1024 ** 3:.1f} GB),上限 4 GB")
    try:
        info = inspect(tmp)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(path)

    info = {"dem_id": dem_id, "filename": filename or path.name,
            "bytes": size, **info}
    _meta_path(dem_id).write_text(json.dumps(info, ensure_ascii=False, indent=1),
                                  encoding="utf-8")
    logger.info("上传地形已保存:%s(%s,%dx%d,%s,%.1f 米分辨率,范围 %s)",
                path.name, info["crs"], info["width"], info["height"],
                info["dtype"], info.get("res_m_approx") or 0.0,
                [round(v, 4) for v in info["bounds_wgs84"]])
    return info


def meta_of(dem_id: str) -> dict | None:
    try:
        p = _meta_path(dem_id)
    except ValueError:
        return None
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def path_of(dem_id: str) -> Path:
    """取上传地形的文件路径,不存在则抛异常。"""
    p = _path_of(dem_id)
    if not (p.exists() and p.stat().st_size > 0):
        raise FileNotFoundError(
            f"上传的地形数据不存在或已过期(id={dem_id})。"
            f"上传文件保留 {UPLOAD_TTL_DAYS} 天,请重新上传后再建任务。")
    return p


def exists(dem_id: str) -> bool:
    try:
        p = _path_of(dem_id)
    except ValueError:
        return False
    return p.exists() and p.stat().st_size > 0


def coverage_ratio(bounds_wgs84, bbox) -> float:
    """上传地形对目标范围的覆盖比例(面积占比,0~1)。

    用于判断是否还需要在线地形兜底,以及界面提示"范围不一致"。
    """
    if not bounds_wgs84 or not bbox:
        return 0.0
    aw, as_, ae, an = bounds_wgs84
    bw, bs, be, bn = bbox
    iw = max(aw, bw); ie = min(ae, be)
    is_ = max(as_, bs); in_ = min(an, bn)
    if ie <= iw or in_ <= is_:
        return 0.0
    target = (be - bw) * (bn - bs)
    if target <= 0:
        return 0.0
    return min((ie - iw) * (in_ - is_) / target, 1.0)
