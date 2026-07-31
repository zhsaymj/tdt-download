"""本地栅格文件作为处理输入源:接收上传、判定数据类型、供导出管线使用。

解决的问题:此前处理能力只对**下载来的**数据开放。工具有两个上传入口,但都是
窄口专用——`buildings/upload` 只能转白模,`buildings/dem/upload` 只能给建筑采样
底面高(其 dem_upload_id 在代码里只被 runner_buildings 消费)。结果是:可以上传
一份实测地形算建筑底面高,却不能用同一份地形出等高线、转 COG、切地形切片,
尽管这些能力工具全都有。

本模块让本地文件走与下载数据同一条导出链路。可行的前提是格式已绑定 DataKind
而非 provider(见 core/formats.py),故只要能从文件判定 kind,stages_for(kind)
就自动给出可用格式。

存储沿用 dem_upload 的目录与流式落盘(不重复实现):同为"上传的栅格",区别只在
用途——那边是建筑管线的辅助高程源,这里是导出管线的输入源。
"""
from __future__ import annotations

from pathlib import Path

from .formats import DataKind
from .logs import logger

#: 判定为高程的浮点 dtype。浮点单波段几乎只用于连续量(高程、温度、指数),
#: 而影像不会用浮点存(存了也无法直接显示)。
_FLOAT_DTYPES = ("float32", "float64")

#: 整型单波段的高程判定阈值。int16/uint16 既可能是高程(米,-500~9000)也可能是
#: 16bit 影像(0~65535 灰度),只看 dtype 分不开,故按值域判断。
_ELEV_MIN = -500.0      # 死海 -430m,留余量
_ELEV_MAX = 9000.0      # 珠峰 8848m


def guess_kind(info: dict, sample_min: float | None = None,
               sample_max: float | None = None) -> tuple[str, str, bool]:
    """从栅格元信息判定 DataKind。

    返回 (kind, 判定依据说明, 是否确定)。**不确定时也返回一个 kind**,但把
    confident 置 False——界面据此提示用户确认。不做"猜不准就报错":多数情况能
    判对,让用户在一个已填好的下拉里改比让他从零选更省事。

    info 来自 dem_upload.inspect(bands / dtype / nodata / ...)。
    sample_min/max 是抽样读到的值域,整型单波段判高程时需要。
    """
    bands = int(info.get("bands") or 0)
    dtype = str(info.get("dtype") or "").lower()

    if bands >= 3:
        return (DataKind.RASTER_IMAGE,
                f"{bands} 波段 {dtype},按彩色影像处理", True)

    if bands == 1:
        if dtype in _FLOAT_DTYPES:
            return (DataKind.RASTER_DEM,
                    f"单波段 {dtype}(浮点),按高程处理", True)
        if dtype == "uint8":
            # 8bit 单波段是灰度图(晕渲、单波段影像),高程放不进 0-255
            return (DataKind.RASTER_IMAGE,
                    "单波段 uint8(0-255),按灰度影像处理", True)
        # int16/uint16/int32:按值域猜,拿不到值域则默认高程(这类整型栅格在
        # GIS 里更常见于 DEM;判错了用户可改)
        if sample_min is None or sample_max is None:
            return (DataKind.RASTER_DEM,
                    f"单波段 {dtype},未取到值域,暂按高程处理", False)
        if _ELEV_MIN <= sample_min and sample_max <= _ELEV_MAX:
            return (DataKind.RASTER_DEM,
                    f"单波段 {dtype},值域 {sample_min:.0f}~{sample_max:.0f} "
                    "落在海拔范围内,按高程处理", True)
        return (DataKind.RASTER_IMAGE,
                f"单波段 {dtype},值域 {sample_min:.0f}~{sample_max:.0f} "
                "超出海拔范围,按灰度影像处理", False)

    if bands == 2:
        # 灰度 + alpha
        return (DataKind.RASTER_IMAGE, f"2 波段 {dtype},按带透明的灰度影像处理", False)

    return (DataKind.RASTER_IMAGE, f"{bands} 波段 {dtype},无法判定,默认影像", False)


def sample_range(path: Path, max_px: int = 512) -> tuple[float, float] | None:
    """抽样读第一波段的值域(降采样读,大图也很快)。

    用于整型单波段的高程判定。忽略 nodata:高程栅格的 nodata 常是 -32768 或
    -9999,算进去会把值域拉出海拔范围、判错类型。
    """
    import numpy as np
    import rasterio

    try:
        with rasterio.open(path) as ds:
            scale = max(ds.width, ds.height) / max_px
            w = max(1, int(ds.width / scale)) if scale > 1 else ds.width
            h = max(1, int(ds.height / scale)) if scale > 1 else ds.height
            arr = ds.read(1, out_shape=(h, w), masked=True)
            if ds.nodata is not None:
                arr = np.ma.masked_equal(arr, ds.nodata)
            if arr.count() == 0:
                return None
            return float(arr.min()), float(arr.max())
    except Exception as e:
        logger.warning("抽样读取值域失败 %s:%s", path.name, e)
        return None


def inspect_for_import(path: Path) -> dict:
    """检查上传的栅格,返回元信息 + 判定出的数据类型 + 可用的导出格式。

    在 dem_upload.inspect 的基础上补三件事:数据类型判定、级别换算、可用格式。
    """
    from . import dem_upload
    from .formats import PIPE_RASTER, stages_for

    info = dem_upload.inspect(path)          # 含 CRS 校验(无 CRS 直接报错)
    rng = sample_range(path)
    kind, reason, confident = guess_kind(
        info, rng[0] if rng else None, rng[1] if rng else None)

    info["kind"] = kind
    info["kind_reason"] = reason
    info["kind_confident"] = confident
    if rng:
        info["value_min"], info["value_max"] = rng
    info["native_level"] = native_level(info, kind)
    # has_tile_cache=False:本地文件没有下载的瓦片缓存,"导出原始 LERC 瓦片"
    # 这类阶段对它不适用
    info["stages"] = [
        {"key": s.key, "label": s.label, "default_on": s.default_on,
         "containers": list(s.containers)}
        for s in stages_for(kind, PIPE_RASTER, has_tile_cache=False)
    ]
    return info


def native_level(info: dict, kind: str) -> int:
    """按分辨率反算该栅格"相当于哪一级瓦片"。

    本地文件只有一个分辨率、没有金字塔,但切瓦片必须知道从哪级切起。这里用与
    下载管线一致的换算:影像走天地图 4326 网格(第 z 级单像素 360/2^z/256 度),
    DEM 走墨卡托(第 z 级单像素 2*20037508/2^z/256 米)。

    取"不低于原始分辨率的最小级别",即宁可多切一级也不损失细节。
    """
    import math

    res = float(info.get("res_native") or 0.0)
    crs = str(info.get("crs") or "").upper()
    if res <= 0:
        return 13                                  # 取不到分辨率时给个常用级别

    if kind == DataKind.RASTER_DEM:
        # 墨卡托网格按米算;源是经纬度时先粗略换成米
        res_m = res * 111320.0 if crs.endswith("4326") else res
        if res_m <= 0:
            return 13
        z = math.log2(2 * 20037508.34 / 256.0 / res_m)
        return max(0, min(16, math.ceil(z)))

    # 影像:天地图 4326 网格按度算;源是投影坐标时先粗略换成度
    res_deg = res / 111320.0 if not crs.endswith("4326") else res
    if res_deg <= 0:
        return 13
    z = math.log2(360.0 / 256.0 / res_deg)
    return max(1, min(18, math.ceil(z)))

