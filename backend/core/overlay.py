"""列出任务成果中"能叠加到二维地图上看"的图层。

设计前提(用户定的方案 A):tif 不做切片服务——能直接叠的就叠,不能的只画范围框并
提示转 COG。理由是转一次换来之后随时可看,而补充导出功能已经能做这件事,
不必再写一个按视野现切的瓦片服务。

各类成果的叠加方式:
  tms/ osm/ 目录   → 前端 XYZ 源直读(/output 已静态挂载)
  *.mbtiles        → 走 /api/tasks/{id}/mbtiles/... 瓦片端点(浏览器读不了 sqlite)
  *.tif            → 前端 ol/source/GeoTIFF 直读。实测 StaticFiles 支持 Range
                     请求(返回 206),这是 geotiff.js 按需读块的前提
  *.geojson        → 前端 VectorSource 直读
  *.gpkg / *.shp   → 浏览器读不了,需后端转 GeoJSON(见 api/tasks 的 vector 端点)
  3dtiles/ terrain → 三维数据,只给"打开预览窗口"入口
"""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote

from .logs import logger

#: 单波段栅格的默认拉伸分位(避免个别极值把整幅压成一片灰)
STRETCH_LO, STRETCH_HI = 2.0, 98.0

#: 成果名里的级别后缀,如 影像下载_z13 / 地形下载_dem_z12。用于把同一数据的
#: 多级成果折叠成一项(见 list_layers)。
_LEVEL_RE = re.compile(r"_z(\d+)")


def _url_of(out_dir: Path, output_root: Path, *parts: str) -> str:
    """成果文件的 HTTP 路径(/output 静态挂载)。中文目录名要 URL 编码。"""
    rel = out_dir.relative_to(output_root).as_posix()
    tail = "/".join(parts)
    return "/output/" + quote(rel) + (("/" + quote(tail)) if tail else "")


def _tile_levels(tiles_dir: Path) -> tuple[int, int] | None:
    """扫瓦片目录的级别范围(目录名即级别)。"""
    zs = []
    try:
        for p in tiles_dir.iterdir():
            if p.is_dir() and p.name.isdigit():
                zs.append(int(p.name))
    except OSError:
        return None
    return (min(zs), max(zs)) if zs else None


def _inspect_raster(path: Path) -> dict:
    """读栅格的叠加所需信息:范围、波段、是否适合直读、单波段的取值范围。"""
    import numpy as np
    import rasterio
    from rasterio.warp import transform_bounds

    with rasterio.open(path) as ds:
        info = {
            "width": ds.width, "height": ds.height, "bands": ds.count,
            "dtype": ds.dtypes[0],
            "crs": str(ds.crs) if ds.crs else "",
            "tiled": bool(ds.profile.get("tiled")),
            "overviews": len(ds.overviews(1)) if ds.count else 0,
        }
        if ds.crs:
            b = ds.bounds
            try:
                info["bounds_wgs84"] = list(transform_bounds(
                    ds.crs, "EPSG:4326", b.left, b.bottom, b.right, b.top,
                    densify_pts=21))
            except Exception:
                info["bounds_wgs84"] = None
        else:
            info["bounds_wgs84"] = None

        # 单波段(高程/灰度)需要拉伸区间才能渲染成可见灰度,否则 3000~4800 的
        # 高程值会被当成 0~255 截断、整幅显示为白。降采样抽样即可,不必读全图。
        if ds.count == 1:
            try:
                scale = max(ds.width, ds.height) / 256
                w = max(1, int(ds.width / scale)) if scale > 1 else ds.width
                h = max(1, int(ds.height / scale)) if scale > 1 else ds.height
                arr = ds.read(1, out_shape=(h, w), masked=True)
                if ds.nodata is not None:
                    arr = np.ma.masked_equal(arr, ds.nodata)
                if arr.count():
                    info["vmin"] = float(np.percentile(arr.compressed(), STRETCH_LO))
                    info["vmax"] = float(np.percentile(arr.compressed(), STRETCH_HI))
            except Exception as e:
                logger.debug("抽样取值域失败 %s:%s", path.name, e)
    return info


def list_layers(task: dict, output_root: Path) -> list[dict]:
    """列出该任务可在地图上叠加(或可打开三维预览)的图层。

    返回的每项含 kind / label / url 等,前端据 kind 决定用哪种 OL 图层:
      tiles  → XYZ 源(url 是 {z}/{x}/{y} 模板)
      cog    → ol/source/GeoTIFF 直读
      raster_only_bbox → 只画范围框(不适合直读,建议转 COG)
      vector → VectorSource(GeoJSON)
      vector_convert → 需后端转 GeoJSON 才能看(gpkg/shp)
      preview3d → 打开三维预览窗口
    """
    out_dir = Path(task.get("output_path") or "")
    if not out_dir.is_dir():
        return []
    try:
        out_dir.relative_to(output_root)
    except ValueError:
        # 成果目录不在 output 根下(手工改过 config),没法用静态 URL 访问
        logger.warning("成果目录不在 output 根下,无法叠加:%s", out_dir)
        return []

    tid = task["id"]
    layers: list[dict] = []

    # ---- 瓦片目录 ----
    for name, label in (("tms", "TMS 瓦片"), ("osm", "OSM 瓦片")):
        d = out_dir / name
        if not d.is_dir():
            continue
        lv = _tile_levels(d)
        # 实际瓦片扩展名:裁剪/注记时是 png,否则可能是 jpg
        ext = "png"
        for p in d.rglob("*"):
            if p.is_file() and p.suffix.lower() in (".png", ".jpg", ".jpeg"):
                ext = p.suffix.lower().lstrip(".")
                break
        layers.append({
            "id": f"dir_{name}", "kind": "tiles", "label": label,
            # grid 与 flip_y 是两件独立的事,不能合成一个 scheme:
            #   grid  = 瓦片网格(geodetic 是 EPSG:4326、0 级 2×1;mercator 同地图)
            #   flip_y= 行号方向是否与 XYZ 相反
            # 目录形态的 TMS 两者都要:geodetic 网格 + 行号自南向北。
            "grid": "geodetic" if name == "tms" else "mercator",
            "flip_y": name == "tms",
            "url": _url_of(out_dir, output_root, name) + "/{z}/{x}/{y}." + ext,
            "minzoom": lv[0] if lv else 0, "maxzoom": lv[1] if lv else 18,
        })

    # ---- MBTiles(浏览器读不了 sqlite,走后端瓦片端点)----
    for kindname, label in (("tms", "TMS 瓦片(MBTiles)"),
                            ("osm", "OSM 瓦片(MBTiles)")):
        p = out_dir / f"{task['name']}_{kindname}.mbtiles"
        if not p.is_file():
            continue
        from .mbtiles import read_metadata
        meta = read_metadata(p)
        layers.append({
            "id": f"mb_{kindname}", "kind": "tiles", "label": label,
            # 网格仍随 tms/osm(tms 包是 geodetic),但 flip_y 恒 False——
            # mbtiles 端点已在后端把 XYZ 的 y 换算成库里的 TMS 行号,
            # 前端再翻一次就成了翻两次,图会上下颠倒。
            "grid": "geodetic" if kindname == "tms" else "mercator",
            "flip_y": False,
            "url": f"/api/tasks/{tid}/mbtiles/{kindname}/{{z}}/{{x}}/{{y}}",
            "minzoom": int(meta.get("minzoom") or 0),
            "maxzoom": int(meta.get("maxzoom") or 18),
        })

    # ---- 栅格文件 ----
    # 按"级别系列"折叠:影像任务勾了 1-18 级会产出 18 张 {name}_z{N}.tif,它们是
    # 同一份数据的不同分辨率,全平铺出来图层面板会长得没法用。同组只出一项,
    # 默认取最高级别(分辨率最高),其余级别放进 levels 供切换。
    groups: dict[str, list[tuple[int, Path]]] = {}
    singles: list[Path] = []
    for p in sorted(out_dir.glob("*.tif")):
        if p.name.startswith("."):
            continue                      # 中间文件
        m = _LEVEL_RE.search(p.stem)
        if m:
            key = p.stem[:m.start()] + p.stem[m.end():]   # 去掉 _z{N} 后的名字
            groups.setdefault(key, []).append((int(m.group(1)), p))
        else:
            singles.append(p)

    def _add_raster(p: Path, label: str, levels: list[dict] | None = None):
        try:
            info = _inspect_raster(p)
        except Exception as e:
            logger.warning("读取栅格失败,跳过 %s:%s", p.name, e)
            return
        # 能否直读:tiled 才能按窗口取;非 tiled 的大图会拖整份数据下来
        big = p.stat().st_size > 80 * 1024 * 1024
        ok = info["tiled"] or not big
        item = {
            "id": "tif_" + p.name, "label": label,
            "kind": "cog" if ok else "raster_only_bbox",
            "url": _url_of(out_dir, output_root, p.name),
            # 绝对路径:供界面「转 COG」时把它当本地文件源来处理(走本地数据入库,
            # 而非补充导出——后端不允许改已导出阶段的容器格式)
            "path": str(p),
            "bytes": p.stat().st_size,
            **info,
        }
        if levels:
            item["levels"] = levels
        layers.append(item)

    for key, items in sorted(groups.items()):
        items.sort(key=lambda t: t[0])
        top_z, top_p = items[-1]
        _add_raster(top_p, f"{key}(z{top_z})" if len(items) == 1
                    else f"{key}(z{items[0][0]}–{top_z},共 {len(items)} 级)",
                    [{"z": z, "url": _url_of(out_dir, output_root, q.name),
                      "label": q.name} for z, q in items])
    for p in singles:
        _add_raster(p, p.name)

    # ---- 矢量文件 ----
    for p in sorted(out_dir.glob("*.geojson")):
        if p.name.startswith("_") or p.name.startswith("."):
            continue
        layers.append({
            "id": "vec_" + p.name, "kind": "vector", "label": p.name,
            "url": _url_of(out_dir, output_root, p.name),
            "bytes": p.stat().st_size,
        })
    for pat in ("*.gpkg", "*.shp"):
        for p in sorted(out_dir.glob(pat)):
            layers.append({
                "id": "vecx_" + p.name, "kind": "vector_convert",
                "label": p.name, "path": str(p),
                "url": f"/api/tasks/{tid}/vector/{quote(p.name)}",
                "bytes": p.stat().st_size,
            })

    # ---- 三维数据:只给预览入口 ----
    for name, label in (("3dtiles", "三维建筑白模(b3dm)"),
                        ("terrain", "Cesium 地形切片")):
        if (out_dir / name).is_dir():
            layers.append({"id": "p3d_" + name, "kind": "preview3d",
                           "label": label})
    return layers
