"""把已写好的 GeoTIFF 转换成用户选定的容器格式(COG / PNG / JPEG / ASCII Grid / XYZ)。

为什么是"先写 GTiff 再转"而不是直接按目标驱动写:拼接是**逐行/逐窗口**写出的
(见 mosaic.mosaic_to_geotiff 的 on_row 回调),而 COG、PNG、AAIGrid 这些驱动都
不支持随机窗口写入——只能整体 CreateCopy。故保留"拼成 GTiff → 转容器"两步。
额外好处是转换失败时原始 GTiff 还在,不会丢成果。

MBTiles 不走这里(它的输入是瓦片目录而非单张栅格),见 mbtiles.py。
"""
from __future__ import annotations

from pathlib import Path

import rasterio
import rasterio.shutil as rio_shutil

from .formats import CONTAINERS
from .logs import logger

#: 各容器的 GDAL 创建选项。
#: PNG/JPEG 必须显式 WORLDFILE=YES 才生成 .wld(实测默认只出 .aux.xml,
#: 而 .aux.xml 是 GDAL 私有格式,别的软件读不到坐标)。
_CREATE_OPTS: dict[str, dict[str, str]] = {
    "cog": {"COMPRESS": "DEFLATE", "BIGTIFF": "IF_SAFER"},
    "png": {"WORLDFILE": "YES", "ZLEVEL": "6"},
    "jpeg": {"WORLDFILE": "YES", "QUALITY": "90"},
    "ascii_grid": {},
    "xyz": {},
}


def convert_raster(src_path: Path, container: str,
                   keep_source: bool = False) -> Path | None:
    """把 src_path(GeoTIFF)转成 container 指定的格式。

    container 为 "gtiff" 或未知值时不做任何事,返回 None(调用方继续用原文件)。
    keep_source=False 时转换成功后删除源 GTiff(容器是"换一种写法",不是"多一份")。
    返回新文件路径;失败时记日志并返回 None(保留源文件,阶段不因此失败)。
    """
    cont = CONTAINERS.get(container)
    if cont is None or container == "gtiff" or cont.writer != "rasterio":
        return None
    if not (src_path.exists() and src_path.stat().st_size > 0):
        return None

    dst_path = src_path.with_suffix(cont.ext)
    # COG 与 GTiff 同为 .tif:原地替换,先写临时文件再改名,避免中途失败留下半成品
    same_name = dst_path == src_path
    tmp_path = src_path.with_suffix(".tmp" + cont.ext) if same_name else dst_path

    try:
        opts = _CREATE_OPTS.get(container, {})
        rio_shutil.copy(str(src_path), str(tmp_path), driver=cont.driver, **opts)
    except Exception as e:
        logger.warning("转换容器格式失败(%s → %s):%s,保留原 GeoTIFF",
                       src_path.name, cont.label, e)
        for p in (tmp_path,) if same_name else ():
            p.unlink(missing_ok=True)
        return None

    if same_name:
        try:
            src_path.unlink(missing_ok=True)
            tmp_path.replace(dst_path)
        except OSError as e:
            logger.warning("替换为 %s 失败:%s", cont.label, e)
            return None
    elif not keep_source:
        try:
            src_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("转换后删除源文件失败(不影响成果):%s", src_path.name)

    logger.info("已转为 %s:%s", cont.label, dst_path.name)
    return dst_path


def container_of(task: dict, stage_key: str) -> str:
    """取某阶段选定的容器 key;未选时返回该阶段注册表里的首项(默认)。"""
    from .formats import STAGES

    chosen = (task.get("containers") or {}).get(stage_key)
    if chosen:
        return chosen
    stage = STAGES.get(stage_key)
    return stage.containers[0] if (stage and stage.containers) else ""


def verify_raster(path: Path) -> bool:
    """确认转换后的文件能被正常打开(容器换错了往往到用户手上才发现)。"""
    try:
        with rasterio.open(path) as ds:
            return ds.width > 0 and ds.height > 0
    except Exception as e:
        logger.warning("成果无法打开:%s(%s)", path.name, e)
        return False
