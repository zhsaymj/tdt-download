"""从 3857 缓存瓦片直映射为 OSM XYZ 瓦片(无损,不重投影)。

与 tms.export_tms 同构,差别:
  ① 网格用 mercator —— OSM XYZ 行列号与 mercator 缓存**完全同向**(已实测),
     无 tms_row 翻转、无 tms_level 换算;
  ② 输出目录 {z}/{x}/{y}.png(即 3857 XYZ 标准)。
"""
from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gdal_env  # noqa: F401
from .annotate import composite_annotation
from .mercator_tiling import mercator_range_for_bbox
from .mosaic import _read_tile
from .tms import _write_tile_png


def export_osm_from_cache(
    provider, tile_path_fn, bbox, levels, out_dir,
    anno_tile_path_fn=None, on_progress=None, should_stop=None,
    concurrency=None,
):
    """把缓存中的 3857 瓦片按 OSM XYZ 规则输出(多线程)。

    tile_path_fn(x, y, z) -> Path:缓存路径函数(Cache 键已与 OSM 同向)。
    anno_tile_path_fn(x, y, z):可选,把注记烘焙进输出(PNG)。
    返回 (out_dir, 已导出级别列表, stopped)。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    stopped = False
    total = sum(mercator_range_for_bbox(*bbox, z).count for z in levels) or 1
    done = 0
    lock = threading.Lock()
    workers = concurrency or min(os.cpu_count() or 4, 8)

    def _bump():
        nonlocal done
        with lock:
            done += 1
            d = done
        if on_progress and d % 16 == 0:
            on_progress(d, total)

    def _one(x, y, z):
        src = tile_path_fn(x, y, z)
        if not (src.exists() and src.stat().st_size > 0):
            return False
        dst = out_dir / str(z) / str(x) / f"{y}.png"
        if dst.exists() and dst.stat().st_size > 0:
            return True
        if anno_tile_path_fn is None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes(src.read_bytes())
            return True
        rgb = _read_tile(src, 3)
        if rgb is None:
            return False
        rgb = composite_annotation(rgb, anno_tile_path_fn(x, y, z))
        import numpy as np
        _write_tile_png(dst, rgb, np.full((256, 256), 255, dtype=np.uint8))
        return True

    exported: set[int] = set()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for z in levels:
            if should_stop and should_stop():
                stopped = True
                break
            tr = mercator_range_for_bbox(*bbox, z)
            futures = []
            for x in range(tr.col_min, tr.col_max + 1):
                for y in range(tr.row_min, tr.row_max + 1):
                    futures.append(pool.submit(_one, x, y, z))
            for f in futures:
                if f.result():
                    exported.add(z)
                _bump()
    if on_progress and not stopped:
        on_progress(total, total)
    return out_dir, sorted(exported), stopped