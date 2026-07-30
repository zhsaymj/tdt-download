"""建筑白模的四叉树分级与 3D Tiles tileset.json 生成。

分级策略(REPLACE 精化):
  - 节点内建筑数 ≤ 阈值 → 叶子,content 收录全部建筑
  - 超过阈值 → 该节点 content 只放**视觉显著**的一批(占地面积×高度 排序取前 N),
    再四分递归;子节点收录其范围内全部建筑
REPLACE 语义下父节点内容会被子节点替换,所以"父放地标、子放全部"正好构成
真实 LOD:远看只加载少量高大建筑,近看才铺满细节。父子几何重复是该模式的常态。

boundingVolume 用 region([west,south,east,north,minH,maxH],弧度),
比 box 直观且不需要算 OBB。

geometricError 逐级折半:根节点给经验值,叶子为 0(0 = 必须加载到此级才算完整)。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from .b3dm import build_b3dm, build_tile_mesh
from .logs import logger

#: 单瓦片建筑数上限,超过则四分
DEFAULT_MAX_PER_TILE = 2000
#: 四叉树最大深度(防止同位置大量重叠建筑导致无限递归)
MAX_DEPTH = 12
#: 根节点 geometricError(米):经验值,约等于"整个范围的可感知尺度"
ROOT_GEOMETRIC_ERROR = 512.0


def _prominence(feat) -> float:
    """视觉显著度:占地面积 × 高度。父级 LOD 挑地标用。"""
    area = float((feat.props or {}).get("area_m2", 0.0))
    return area * float(feat.height or 0.0)


def _feat_center(feat) -> tuple[float, float]:
    outer = feat.rings[0]
    n = len(outer)
    return (sum(p[0] for p in outer) / n, sum(p[1] for p in outer) / n)


def _bounds_of(buildings) -> tuple[float, float, float, float, float, float]:
    """建筑集合的经纬度包围盒 + 高程区间。"""
    minx = miny = 1e18
    maxx = maxy = -1e18
    minh = 1e18
    maxh = -1e18
    for f in buildings:
        for lon, lat in f.rings[0]:
            minx = min(minx, lon); maxx = max(maxx, lon)
            miny = min(miny, lat); maxy = max(maxy, lat)
        b = float(f.base_height)
        minh = min(minh, b)
        maxh = max(maxh, b + float(f.height or 0.0))
    return minx, miny, maxx, maxy, minh, maxh


def _region(minx, miny, maxx, maxy, minh, maxh) -> list[float]:
    """3D Tiles region boundingVolume:经纬度用弧度,高度用米。"""
    return [
        math.radians(minx), math.radians(miny),
        math.radians(maxx), math.radians(maxy),
        float(minh), float(maxh),
    ]


class _Node:
    __slots__ = ("buildings", "depth", "children", "content_uri", "bounds", "geom_error")

    def __init__(self, buildings, depth: int):
        self.buildings = buildings
        self.depth = depth
        self.children: list[_Node] = []
        self.content_uri: str | None = None
        self.bounds = None
        self.geom_error = 0.0


def _split(buildings, minx, miny, maxx, maxy):
    """按范围中心把建筑分到四个象限(以建筑中心点归属,保证不重不漏)。"""
    cx = (minx + maxx) * 0.5
    cy = (miny + maxy) * 0.5
    quads = [[], [], [], []]
    for f in buildings:
        fx, fy = _feat_center(f)
        i = (0 if fx < cx else 1) + (0 if fy < cy else 2)
        quads[i].append(f)
    return [q for q in quads if q]


def build_tree(buildings, max_per_tile: int = DEFAULT_MAX_PER_TILE) -> _Node:
    """构建四叉树,返回根节点(此时尚未写盘,content_uri 为空)。"""
    root = _Node(buildings, 0)
    stack = [root]
    node_count = 0
    while stack:
        node = stack.pop()
        node_count += 1
        minx, miny, maxx, maxy, minh, maxh = _bounds_of(node.buildings)
        node.bounds = (minx, miny, maxx, maxy, minh, maxh)
        node.geom_error = ROOT_GEOMETRIC_ERROR / (2 ** node.depth)

        if len(node.buildings) <= max_per_tile or node.depth >= MAX_DEPTH:
            continue        # 叶子:content 为全部建筑

        quads = _split(node.buildings, minx, miny, maxx, maxy)
        # 分不开(全挤在一个象限且深度未到上限)→ 当叶子处理,避免死循环
        if len(quads) <= 1:
            continue
        for q in quads:
            child = _Node(q, node.depth + 1)
            node.children.append(child)
            stack.append(child)
    logger.info("建筑四叉树:%d 个节点,根含 %d 栋,单瓦片上限 %d",
                node_count, len(buildings), max_per_tile)
    return root


def _node_content_buildings(node: _Node, max_per_tile: int):
    """该节点 content 实际要写入的建筑。

    叶子:全部。中间节点:按显著度取前 max_per_tile 栋(父级 LOD)。
    """
    if not node.children:
        return node.buildings
    top = sorted(node.buildings, key=_prominence, reverse=True)[:max_per_tile]
    return top


def export_tileset(
    buildings,
    out_dir: Path,
    *,
    max_per_tile: int = DEFAULT_MAX_PER_TILE,
    on_progress=None,
    should_stop=None,
) -> tuple[Path, int, bool]:
    """写出 3D Tiles 瓦片集(tileset.json + tiles/*.b3dm)。

    返回 (tileset.json 路径, 已写瓦片数, 是否被中断)。
    中断时已写的瓦片保留,tileset.json 不写(避免残缺清单被当成完整成果)。
    """
    if not buildings:
        raise RuntimeError("没有可用建筑,无法生成 3D Tiles")

    out_dir.mkdir(parents=True, exist_ok=True)
    tiles_dir = out_dir / "tiles"
    tiles_dir.mkdir(parents=True, exist_ok=True)

    root = build_tree(buildings, max_per_tile)

    # 广度遍历分配 uri 并写 b3dm
    nodes: list[_Node] = []
    queue = [root]
    while queue:
        n = queue.pop(0)
        nodes.append(n)
        queue.extend(n.children)

    total = len(nodes)
    written = 0
    for i, node in enumerate(nodes):
        if should_stop and should_stop():
            logger.info("3D Tiles 切片被中断,已写 %d/%d 个瓦片", written, total)
            return (out_dir / "tileset.json", written, True)

        subset = _node_content_buildings(node, max_per_tile)
        mesh = build_tile_mesh(subset)
        if mesh is not None:
            name = f"t_{node.depth}_{i}.b3dm"
            data, _center = build_b3dm(mesh)
            (tiles_dir / name).write_bytes(data)
            node.content_uri = f"tiles/{name}"
            written += 1
        if on_progress:
            on_progress(i + 1, total)

    tileset = {
        "asset": {"version": "1.0", "tilesetVersion": "tianditu-downloader/buildings"},
        "geometricError": ROOT_GEOMETRIC_ERROR,
        "root": _node_json(root),
    }
    path = out_dir / "tileset.json"
    path.write_text(json.dumps(tileset, ensure_ascii=False, indent=1), encoding="utf-8")
    logger.info("3D Tiles 写出完成:%d 个 b3dm,%s", written, path.name)
    return path, written, False


def _node_json(node: _Node) -> dict:
    """递归生成 tileset 节点 JSON。"""
    d: dict = {
        "boundingVolume": {"region": _region(*node.bounds)},
        # 叶子 geometricError 归零:表示到此级即为最终细节
        "geometricError": 0.0 if not node.children else node.geom_error,
        "refine": "REPLACE",
    }
    if node.content_uri:
        d["content"] = {"uri": node.content_uri}
    if node.children:
        d["children"] = [_node_json(c) for c in node.children]
    return d
