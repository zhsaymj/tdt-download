"""把已切好的瓦片目录打包成单个 MBTiles(sqlite)文件。

为什么手写而不用 GDAL 的 MBTiles 驱动:驱动在 GDAL 驱动列表里,但 rasterio 的
create 路径未适配它——实测 `rasterio.open(p,'w',driver='MBTiles',...)` 抛
`TypeError: not all arguments converted during string formatting`。而 MBTiles
本质就是几张表的 sqlite,标准库 sqlite3 足够,且瓦片文件都已生成好,只是入库。

规范:https://github.com/mapbox/mbtiles-spec — 必须有 metadata 与 tiles 两张表,
tiles 的行号 tile_row 是 **TMS 约定(自南向北)**。

行号处理是唯一容易错的地方:
  - TMS 瓦片目录本身就是 {L}/{tx}/{ty}.png、ty 自南向北 → 直接入库
  - OSM/XYZ 瓦片目录是 {z}/{x}/{y}.png、y 自北向南 → 必须翻转 y = 2^z-1-y
翻转错了地图会上下颠倒,而单看某一张瓦片是正常的,极难发现。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .logs import logger

_SCHEMA = """
CREATE TABLE IF NOT EXISTS metadata (name TEXT, value TEXT);
CREATE TABLE IF NOT EXISTS tiles (
    zoom_level INTEGER, tile_column INTEGER, tile_row INTEGER, tile_data BLOB
);
CREATE UNIQUE INDEX IF NOT EXISTS tile_index
    ON tiles (zoom_level, tile_column, tile_row);
"""


def pack_mbtiles(tiles_dir: Path, out_path: Path, *, scheme: str,
                 name: str = "", bbox: tuple[float, float, float, float] | None = None,
                 tile_format: str = "png", should_stop=None,
                 on_progress=None) -> Path | None:
    """把 tiles_dir 下的 {z}/{x}/{y}.{ext} 打包成 MBTiles。

    scheme: "tms"(目录行号已自南向北,直接入库)或 "xyz"(自北向南,需翻转)。
    返回输出路径;目录为空时返回 None。
    """
    if scheme not in ("tms", "xyz"):
        raise ValueError(f"未知的瓦片行号约定:{scheme}")
    if not tiles_dir.is_dir():
        return None

    files = [p for p in tiles_dir.rglob(f"*.{tile_format}") if p.is_file()]
    if not files:
        logger.warning("MBTiles:%s 下没有 .%s 瓦片,跳过打包", tiles_dir, tile_format)
        return None

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.unlink(missing_ok=True)      # 重跑时重建,避免与旧内容混合

    con = sqlite3.connect(str(out_path))
    try:
        con.executescript(_SCHEMA)
        zooms: set[int] = set()
        done = 0
        rows: list[tuple[int, int, int, bytes]] = []
        for path in files:
            if should_stop and should_stop():
                con.rollback()
                out_path.unlink(missing_ok=True)
                return None
            try:
                z = int(path.parent.parent.name)
                x = int(path.parent.name)
                y = int(path.stem)
            except ValueError:
                continue          # 不符合 {z}/{x}/{y} 结构的文件(如 tilemapresource.xml)
            # XYZ 的 y 自北向南,MBTiles 要求自南向北
            tile_row = y if scheme == "tms" else (1 << z) - 1 - y
            rows.append((z, x, tile_row, path.read_bytes()))
            zooms.add(z)
            done += 1
            if len(rows) >= 500:
                con.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", rows)
                rows.clear()
                if on_progress:
                    on_progress(done, len(files))
        if rows:
            con.executemany("INSERT OR REPLACE INTO tiles VALUES (?,?,?,?)", rows)

        meta = {
            "name": name or out_path.stem,
            "format": "jpg" if tile_format in ("jpg", "jpeg") else tile_format,
            "type": "baselayer",
            "version": "1.0",
            "description": f"由天地图下载处理工具导出({scheme.upper()} 网格)",
        }
        if zooms:
            meta["minzoom"] = str(min(zooms))
            meta["maxzoom"] = str(max(zooms))
        if bbox:
            meta["bounds"] = ",".join(f"{v:.8f}" for v in bbox)
            meta["center"] = (f"{(bbox[0]+bbox[2])/2:.8f},"
                              f"{(bbox[1]+bbox[3])/2:.8f},{max(zooms) if zooms else 0}")
        # TMS 网格是 EPSG:4326 geodetic,与 MBTiles 默认的墨卡托不同,记进元数据备查
        if scheme == "tms":
            meta["crs"] = "EPSG:4326"
            meta["profile"] = "geodetic"
        con.executemany("INSERT INTO metadata VALUES (?,?)",
                        [(k, str(v)) for k, v in meta.items()])
        con.commit()
    finally:
        con.close()

    if on_progress:
        on_progress(done, len(files))
    logger.info("MBTiles:已打包 %s(%d 张瓦片,级别 %s)",
                out_path.name, done,
                f"{min(zooms)}-{max(zooms)}" if zooms else "无")
    return out_path
