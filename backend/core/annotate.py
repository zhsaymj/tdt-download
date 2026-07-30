"""路网注记叠加:把天地图注记瓦片(cia/cva/cta,带 alpha 的 PNG)
按 alpha `over` 合成到底图瓦片(影像/矢量/地形)之上。

三条导出链路复用同一套逻辑:
  - GeoTIFF 拼接(mosaic):逐瓦片读底图 → 合成注记 → 落到画布
  - TMS 导出:注记开启时逐瓦片解码合成(输出 PNG)
  - OSM 导出:源用已烘焙注记的 GeoTIFF,天然继承,无需单独处理

注记瓦片多为 RGBA PNG(部分为带透明的调色板 PNG),这里统一解出
(rgb, alpha) 再做合成。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import rasterio
from rasterio.enums import ColorInterp

TILE_SIZE = 256


def read_annotation_rgba(path: Path) -> tuple[np.ndarray, np.ndarray] | None:
    """读注记瓦片为 (rgb(3,256,256), alpha(256,256)) uint8;失败返回 None。

    兼容 RGBA PNG 与带透明通道的调色板 PNG。
    """
    if not (path.exists() and path.stat().st_size > 0):
        return None
    try:
        with rasterio.open(path) as src:
            arr = src.read(out_shape=(src.count, TILE_SIZE, TILE_SIZE))
            is_palette = (
                src.count == 1
                and src.colorinterp[0] == ColorInterp.palette
            )
            colormap = src.colormap(1) if is_palette else None
    except Exception:
        return None

    if colormap is not None:
        # 调色板 + 透明:色表项为 (R,G,B,A),展开为 RGBA
        idx = arr[0].astype(np.uint8)
        lut = np.zeros((256, 4), dtype=np.uint8)
        for k, v in colormap.items():
            if 0 <= k < 256:
                rgba = list(v[:4]) + [255] * (4 - len(v[:4]))
                lut[k] = rgba[:4]
        rgba = lut[idx]                          # (256,256,4)
        rgb = np.transpose(rgba[:, :, :3], (2, 0, 1))
        alpha = rgba[:, :, 3]
        return rgb.astype(np.uint8), alpha.astype(np.uint8)

    count = arr.shape[0]
    if count >= 4:
        return arr[:3].astype(np.uint8), arr[3].astype(np.uint8)
    if count == 3:
        # 无 alpha:视为不透明(理论上天地图注记不会走到这里)
        alpha = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
        return arr[:3].astype(np.uint8), alpha
    # 单波段灰度:复制为 RGB,不透明
    rgb = np.repeat(arr[:1], 3, axis=0).astype(np.uint8)
    alpha = np.full((TILE_SIZE, TILE_SIZE), 255, dtype=np.uint8)
    return rgb, alpha


def composite_over(base_rgb: np.ndarray, anno_rgb: np.ndarray,
                   anno_alpha: np.ndarray) -> np.ndarray:
    """alpha `over` 合成:注记叠在底图上。

    base_rgb / anno_rgb: (3,H,W) uint8;anno_alpha: (H,W) uint8。
    返回合成后的 (3,H,W) uint8。
    """
    a = anno_alpha.astype(np.float32) / 255.0        # (H,W)
    a = a[np.newaxis, :, :]                           # (1,H,W) 广播到 3 波段
    out = base_rgb.astype(np.float32) * (1.0 - a) + anno_rgb.astype(np.float32) * a
    return np.clip(out, 0, 255).astype(np.uint8)


def composite_annotation(base_rgb: np.ndarray, anno_path: Path) -> np.ndarray:
    """把 anno_path 处的注记瓦片合成到 base_rgb(3,256,256)之上。

    注记瓦片缺失或无法解码时,原样返回 base_rgb(不影响底图)。
    """
    anno = read_annotation_rgba(anno_path)
    if anno is None:
        return base_rgb
    anno_rgb, anno_alpha = anno
    if not anno_alpha.any():
        return base_rgb
    return composite_over(base_rgb, anno_rgb, anno_alpha)
