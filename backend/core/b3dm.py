"""建筑白模几何生成 + glTF/GLB/b3dm 二进制编码。

管线:footprint(经纬度环) → 挤出为三维实体 → ECEF 局部坐标 → glTF → GLB → b3dm

**为何手写二进制**:b3dm 只是 28 字节头 + 两段 JSON + 一段 GLB;GLB 也只是
12 字节头 + JSON chunk + BIN chunk。手写约两百行即可完全控制顶点精度与
Batch Table,比引入 trimesh/pygltflib 更轻且可控。

**精度关键点**:ECEF 坐标量级 6.4e6 米,而 glTF 顶点是 float32(约 7 位有效
十进制数字),直接写绝对坐标会有分米级抖动、建筑看起来毛糙。标准解法是
每个瓦片的顶点用**相对该瓦片原点的局部坐标**(数值降到千米级),绝对位置
放进 tileset.json 的 transform 矩阵(float64)。本模块 build_tile_glb 返回
GLB 与对应的 RTC 中心,由 tileset3d 写入 transform。
"""
from __future__ import annotations

import json
import struct

import numpy as np

# WGS84 椭球参数
WGS84_A = 6378137.0
WGS84_F = 1.0 / 298.257223563
WGS84_E2 = WGS84_F * (2.0 - WGS84_F)

# 侧墙与屋顶颜色(RGB,0-255)。用顶点色而非贴图,成果自包含、体积小。
WALL_COLOR = (216, 220, 226)
ROOF_COLOR = (176, 186, 198)


def lonlat_to_ecef(lon: float, lat: float, h: float) -> tuple[float, float, float]:
    """经纬度 + 椭球高 → ECEF(地心地固直角坐标,米)。"""
    lam = np.radians(lon)
    phi = np.radians(lat)
    sin_phi, cos_phi = np.sin(phi), np.cos(phi)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_phi * sin_phi)
    x = (n + h) * cos_phi * np.cos(lam)
    y = (n + h) * cos_phi * np.sin(lam)
    z = (n * (1.0 - WGS84_E2) + h) * sin_phi
    return float(x), float(y), float(z)


def _ecef_batch(lons: np.ndarray, lats: np.ndarray, hs: np.ndarray) -> np.ndarray:
    """向量化的经纬度→ECEF,返回 (N,3) float64。"""
    lam = np.radians(lons)
    phi = np.radians(lats)
    sin_phi, cos_phi = np.sin(phi), np.cos(phi)
    n = WGS84_A / np.sqrt(1.0 - WGS84_E2 * sin_phi * sin_phi)
    x = (n + hs) * cos_phi * np.cos(lam)
    y = (n + hs) * cos_phi * np.sin(lam)
    z = (n * (1.0 - WGS84_E2) + hs) * sin_phi
    return np.stack([x, y, z], axis=1)


# ---------- 单栋建筑挤出 ----------

def _ring_is_ccw(ring: list[tuple[float, float]]) -> bool:
    """鞋带法判断环走向是否逆时针(用于统一侧墙法线朝外)。"""
    s = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s > 0.0


def _triangulate_footprint(rings: list[list[tuple[float, float]]]) -> np.ndarray:
    """带洞多边形三角化,返回外环+洞拼接后顶点序列中的三角索引 (M,3)。

    用 mapbox_earcut:面向平面多边形的耳切算法,正确处理洞。
    footprint 在建筑尺度上可直接当平面处理(经纬度当平面坐标即可,
    三角剖分的拓扑与坐标缩放无关)。
    """
    import mapbox_earcut as earcut

    verts: list[tuple[float, float]] = []
    ring_ends: list[int] = []
    for ring in rings:
        verts.extend(ring)
        ring_ends.append(len(verts))
    arr = np.array(verts, dtype=np.float64)
    idx = earcut.triangulate_float64(arr, ring_ends)
    if len(idx) < 3:
        return np.zeros((0, 3), dtype=np.uint32)
    return np.asarray(idx, dtype=np.uint32).reshape(-1, 3)


def build_building_mesh(rings, base_h: float, top_h: float):
    """把一栋建筑的 footprint 挤出成三维网格。

    返回 (positions_lonlath: (N,3) float64 的 [lon,lat,h], indices: (M,3) uint32,
          colors: (N,3) uint8)。

    构成:顶面(屋顶,三角化) + 底面(省略——建筑贴地不可见,省一半面数) +
    侧墙(每条边两个三角)。为让侧墙有独立法线着色,墙面顶点不与屋顶共享。
    """
    roof_tris = _triangulate_footprint(rings)

    pos: list[tuple[float, float, float]] = []
    col: list[tuple[int, int, int]] = []
    tris: list[tuple[int, int, int]] = []

    # --- 屋顶面:所有环的顶点按 rings 顺序排布,与三角化时的索引一致 ---
    roof_base_idx = 0
    for ring in rings:
        for lon, lat in ring:
            pos.append((lon, lat, top_h))
            col.append(ROOF_COLOR)
    for a, b, c in roof_tris:
        tris.append((roof_base_idx + int(a), roof_base_idx + int(b), roof_base_idx + int(c)))

    # --- 侧墙:逐环逐边生成独立四边形(两个三角)---
    for ring in rings:
        n = len(ring)
        ccw = _ring_is_ccw(ring)
        for i in range(n):
            lon1, lat1 = ring[i]
            lon2, lat2 = ring[(i + 1) % n]
            b = len(pos)
            # 四个角:下1 下2 上2 上1
            pos.append((lon1, lat1, base_h)); col.append(WALL_COLOR)
            pos.append((lon2, lat2, base_h)); col.append(WALL_COLOR)
            pos.append((lon2, lat2, top_h));  col.append(WALL_COLOR)
            pos.append((lon1, lat1, top_h));  col.append(WALL_COLOR)
            # 逆时针环:外法线朝外的绕序为 (b,b+1,b+2)/(b,b+2,b+3);顺时针环反向
            if ccw:
                tris.append((b, b + 1, b + 2))
                tris.append((b, b + 2, b + 3))
            else:
                tris.append((b, b + 2, b + 1))
                tris.append((b, b + 3, b + 2))

    return (np.array(pos, dtype=np.float64),
            np.array(tris, dtype=np.uint32).reshape(-1, 3),
            np.array(col, dtype=np.uint8))


# ---------- 瓦片级合批 ----------

def build_tile_mesh(buildings):
    """把一批建筑合并成单个瓦片网格(带 _BATCHID,支持逐栋拾取)。

    返回 dict:
      positions_ecef (N,3) float64 绝对 ECEF
      indices (M*3,) uint32
      colors (N,3) uint8
      batch_ids (N,) float32   每个顶点所属建筑序号
      batch_table: {ids, heights, names, base_heights, ...}
    """
    all_pos = []
    all_idx = []
    all_col = []
    all_bid = []
    bt_ids, bt_h, bt_names, bt_base, bt_src, bt_area = [], [], [], [], [], []
    # 用户自选保留字段:逐栋收集,最后按并集补齐(Batch Table 要求每个字段的
    # 数组长度都等于 batch_length,缺值必须占位而不能省略)
    extra_rows: list[dict] = []

    voffset = 0
    for bidx, feat in enumerate(buildings):
        base = float(feat.base_height)
        top = base + float(feat.height or 0.0)
        if top <= base:
            continue
        try:
            pos, tris, col = build_building_mesh(feat.rings, base, top)
        except Exception:
            continue        # 单栋三角化失败:跳过,不影响整瓦片
        if len(pos) == 0 or len(tris) == 0:
            continue

        lons = pos[:, 0]
        lats = pos[:, 1]
        hs = pos[:, 2]
        all_pos.append(_ecef_batch(lons, lats, hs))
        all_idx.append(tris + voffset)
        all_col.append(col)
        all_bid.append(np.full(len(pos), bidx, dtype=np.float32))
        voffset += len(pos)

        bt_ids.append(feat.fid)
        bt_h.append(round(float(feat.height or 0.0), 2))
        bt_names.append(feat.name or "")
        bt_base.append(round(base, 2))
        bt_src.append(feat.height_source or "")
        props = feat.props or {}
        bt_area.append(float(props.get("area_m2", 0.0)))
        # area_m2 已由 areaM2 暴露,不再重复
        extra_rows.append({k: v for k, v in props.items() if k != "area_m2"})

    if not all_pos:
        return None

    batch_table = {
        "id": bt_ids,
        "height": bt_h,
        "name": bt_names,
        "baseHeight": bt_base,
        "heightSource": bt_src,
        "areaM2": bt_area,
    }
    batch_table.update(_align_extra_fields(extra_rows, len(bt_ids), set(batch_table)))

    return {
        "positions_ecef": np.concatenate(all_pos, axis=0),
        "indices": np.concatenate(all_idx, axis=0).reshape(-1),
        "colors": np.concatenate(all_col, axis=0),
        "batch_ids": np.concatenate(all_bid, axis=0),
        "batch_length": len(bt_ids),
        "batch_table": batch_table,
    }


def _align_extra_fields(rows: list[dict], n: int, reserved: set) -> dict:
    """把逐栋的自选字段整理成等长列名数组。

    - 取所有行的键并集(不同要素可能字段不全)
    - 缺失值补 None(数值列)或 ""(文本列),保证每列长度都等于 n
    - 与固定字段同名时加 _ 后缀,避免覆盖 id/height 等
    - 值统一为 JSON 可序列化:数值保留,其余转字符串
    """
    keys: list[str] = []
    seen = set()
    for r in rows:
        for k in r:
            if k not in seen:
                seen.add(k)
                keys.append(k)
    if not keys:
        return {}

    out: dict[str, list] = {}
    for k in keys:
        # 该列是否整体为数值(空值不参与判定)
        numeric = True
        for r in rows:
            v = r.get(k)
            if v is None or v == "":
                continue
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                numeric = False
                break
        col = []
        for r in rows:
            v = r.get(k)
            if v is None or v == "":
                col.append(None if numeric else "")
            elif numeric:
                col.append(float(v) if isinstance(v, float) else v)
            else:
                col.append(v if isinstance(v, str) else str(v))
        # 列名去重:与固定字段冲突时加后缀
        name = k if k not in reserved else f"{k}_"
        while name in out:
            name += "_"
        out[name] = col + [None if numeric else ""] * (n - len(col))
    return out


# ---------- GLB 编码 ----------

def _pad4(b: bytes, pad_byte: bytes = b"\x00") -> bytes:
    r = len(b) % 4
    return b if r == 0 else b + pad_byte * (4 - r)


def build_glb(mesh: dict) -> tuple[bytes, tuple[float, float, float]]:
    """把瓦片网格编码为 GLB(glTF 2.0 二进制),返回 (glb_bytes, rtc_center)。

    顶点写**相对 rtc_center 的局部坐标**以规避 float32 精度问题;
    rtc_center 为该瓦片 ECEF 包围盒中心,由调用方写入 tileset transform。

    glTF 的 Y 轴向上约定与 ECEF 的 Z 轴向上不同,3D Tiles 规范要求
    b3dm 内的 glTF 用 Y-up,故这里做一次 ECEF(Z-up) → glTF(Y-up) 轴变换:
    (x, y, z)_ecef → (x, z, -y)_gltf。
    """
    pos_ecef = mesh["positions_ecef"]
    center = pos_ecef.mean(axis=0)
    local = pos_ecef - center

    # ECEF(Z-up) → glTF(Y-up)
    verts = np.stack([local[:, 0], local[:, 2], -local[:, 1]], axis=1).astype(np.float32)
    indices = mesh["indices"].astype(np.uint32)
    colors = mesh["colors"].astype(np.uint8)
    batch_ids = mesh["batch_ids"].astype(np.float32)

    # 顶点色需 4 分量(RGBA)的 unsigned byte normalized
    rgba = np.concatenate(
        [colors, np.full((len(colors), 1), 255, dtype=np.uint8)], axis=1
    )

    # --- BIN chunk:各 accessor 数据依次 4 字节对齐拼接 ---
    parts: list[bytes] = []
    offsets: list[int] = []
    cursor = 0

    def add(arr: np.ndarray) -> int:
        nonlocal cursor
        raw = _pad4(arr.tobytes())
        parts.append(raw)
        off = cursor
        offsets.append(off)
        cursor += len(raw)
        return off

    off_pos = add(verts)
    off_idx = add(indices)
    off_col = add(rgba)
    off_bid = add(batch_ids)
    bin_blob = b"".join(parts)

    vmin = verts.min(axis=0).tolist()
    vmax = verts.max(axis=0).tolist()

    gltf = {
        "asset": {"version": "2.0", "generator": "tianditu-downloader/b3dm"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        # 顶点已在上面做了 Y-up 变换,这里不再加矩阵
        "nodes": [{"mesh": 0, "name": "buildings"}],
        "meshes": [{
            "primitives": [{
                "attributes": {"POSITION": 0, "COLOR_0": 2, "_BATCHID": 3},
                "indices": 1,
                "material": 0,
                "mode": 4,      # TRIANGLES
            }],
        }],
        "materials": [{
            "name": "building",
            "pbrMetallicRoughness": {
                "baseColorFactor": [1.0, 1.0, 1.0, 1.0],
                "metallicFactor": 0.0,
                "roughnessFactor": 0.9,
            },
            "doubleSided": True,
        }],
        "buffers": [{"byteLength": len(bin_blob)}],
        "bufferViews": [
            {"buffer": 0, "byteOffset": off_pos, "byteLength": verts.nbytes, "target": 34962},
            {"buffer": 0, "byteOffset": off_idx, "byteLength": indices.nbytes, "target": 34963},
            {"buffer": 0, "byteOffset": off_col, "byteLength": rgba.nbytes, "target": 34962},
            {"buffer": 0, "byteOffset": off_bid, "byteLength": batch_ids.nbytes, "target": 34962},
        ],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": len(verts),
             "type": "VEC3", "min": vmin, "max": vmax},
            {"bufferView": 1, "componentType": 5125, "count": len(indices),
             "type": "SCALAR"},
            {"bufferView": 2, "componentType": 5121, "count": len(rgba),
             "type": "VEC4", "normalized": True},
            {"bufferView": 3, "componentType": 5126, "count": len(batch_ids),
             "type": "SCALAR"},
        ],
    }

    json_chunk = _pad4(json.dumps(gltf, separators=(",", ":")).encode("utf-8"), b" ")
    bin_chunk = _pad4(bin_blob)

    total = 12 + 8 + len(json_chunk) + 8 + len(bin_chunk)
    out = bytearray()
    out += struct.pack("<III", 0x46546C67, 2, total)          # "glTF", version 2
    out += struct.pack("<II", len(json_chunk), 0x4E4F534A)    # JSON chunk
    out += json_chunk
    out += struct.pack("<II", len(bin_chunk), 0x004E4942)     # BIN chunk
    out += bin_chunk
    return bytes(out), (float(center[0]), float(center[1]), float(center[2]))


# ---------- b3dm 封装 ----------

def build_b3dm(mesh: dict) -> tuple[bytes, tuple[float, float, float]]:
    """把瓦片网格编码为 b3dm,返回 (b3dm_bytes, rtc_center)。

    b3dm 结构:
      28 字节头(magic/version/byteLength/FT JSON len/FT bin len/BT JSON len/BT bin len)
      + Feature Table JSON + Feature Table binary
      + Batch Table JSON + Batch Table binary
      + GLB
    各段均需 8 字节对齐(规范要求 GLB 起始 8 字节对齐)。
    """
    glb, center = build_glb(mesh)

    # RTC_CENTER:glTF 内顶点是相对该点的局部坐标,Cesium 据此还原绝对位置。
    # 用它而不是 tileset.json 的 transform,好处是每个 b3dm 自包含。
    # 还原关系:p_ecef = RTC_CENTER + Yup→Zup(v_gltf),与 build_glb 的
    # (x,y,z)_ecef → (x,z,-y)_gltf 互为逆变换。
    feature_table = {
        "BATCH_LENGTH": int(mesh["batch_length"]),
        "RTC_CENTER": [center[0], center[1], center[2]],
    }
    batch_table = mesh["batch_table"]

    ft_json = json.dumps(feature_table, separators=(",", ":")).encode("utf-8")
    bt_json = json.dumps(batch_table, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

    def pad_to(b: bytes, target_mod: int, pad=b" ") -> bytes:
        """用空格把 b 补到长度 ≡ target_mod (mod 8)。"""
        need = (target_mod - len(b)) % 8
        return b + pad * need

    # GLB 必须落在 8 字节边界。头长 28 ≡ 4 (mod 8),故让 FT 补到 8 的倍数、
    # BT 补到 ≡ 4 (mod 8),则 28 + ft + bt ≡ 0 (mod 8)。
    header_len = 28
    ft_json = pad_to(ft_json, 0)
    bt_json = pad_to(bt_json, 4)

    total = header_len + len(ft_json) + len(bt_json) + len(glb)
    out = bytearray()
    out += b"b3dm"
    out += struct.pack("<IIIIII", 1, total, len(ft_json), 0, len(bt_json), 0)
    out += ft_json
    out += bt_json
    out += glb
    return bytes(out), center
