"""扫描本地目录，识别出可发布为数据服务的成果。

判据取自**格式规范定义的标志文件**，不是目录命名约定——因此对第三方瓦片包
同样成立：

  模型   tileset.json（3D Tiles 规范）
  地形   layer.json（Cesium quantized-mesh 规范）
  影像   tilemapresource.xml（gdal2tiles）或数字分层瓦片子目录
  矢量   *.geojson / *.kml 文件

扫不出的常见情况：既没有判据文件、又没有数字分层瓦片目录的数据。此类回退到
手工指定类型注册（接口接受显式 kind）。

**默认候选扫文件系统而非读数据库**：实测库中 106 条 output_path 记录有 100 条
目录已不存在（成果被清理但记录留存），读库会生成 100 个死服务。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from .logs import logger
from .service_bounds import (
    _COLROW_RE, bounds_from_layer_json, bounds_from_metadata,
    bounds_from_tile_index, bounds_from_tilemapresource, bounds_from_tileset,
    bounds_for_vector, detect_grid,
)

#: 递归扫描深度上限。
#:
#: 原为 3,依据是"成果目录不超过 3 层(output/<任务名>/<成果类型>/<文件>)"
#: —— 这个假设对本工具**自己的影像导出布局**不成立:
#:
#:     output/<任务名>/tms/<z>/<x>/<y>.png     ← 瓦片文件在第 4 层
#:
#: 而 `_Tree.tile_roots()` 是**从瓦片文件反推**瓦片根的(见它的说明),
#: 看不到文件就认不出影像。实测 2026-09-29:MAX_DEPTH=3 时扫 output/ 得到
#: 6 个候选、**影像 0 个**,10 个已下载的影像成果全部漏掉;放到 4 就是 10 个。
#:
#: 代价实测(同一份 output,2972 个目录 / 22 万个文件):0.47s → 2.10s ——
#: 第 4 层有 2734 个 x 目录要列。接口走 asyncio.to_thread,不阻塞事件循环。
#: 4 已足够:试到 5、6 结果与 4 完全一致(16 个候选)。
MAX_DEPTH = 4

#: 候选的四个分类（与前端 utils/provider.js 的 SERVICE_KINDS 一致）
KIND_MODEL = "model"
KIND_IMAGERY = "imagery"
KIND_VECTOR = "vector"
KIND_TERRAIN = "terrain"

#: 分类 -> 展示用的中文名
KIND_LABELS = {
    KIND_MODEL: "模型", KIND_IMAGERY: "影像",
    KIND_VECTOR: "矢量", KIND_TERRAIN: "地形",
}

#: 矢量文件扩展名
_VECTOR_SUFFIXES = {".geojson", ".kml"}
#: 瓦片文件扩展名
_TILE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class Candidate:
    """一个可发布为服务的成果。字段与 services 表一一对应。"""
    kind: str
    root: str                 # 绝对路径（已 resolve）
    entry: str = ""           # 相对 root 的入口；瓦片/地形为 ""
    label: str = ""
    grid: str = ""            # 'geodetic' / 'mercator' / ''（非瓦片）
    flip_y: bool = False
    minzoom: int = 0
    maxzoom: int = 18
    bounds_wgs84: list[float] | None = None
    bounds_approx: bool = False
    tile_ext: str = "png"     # 瓦片扩展名（拼访问地址用）
    extra: dict = field(default_factory=dict)


def _join(rel: str, name: str) -> str:
    return "/".join(x for x in (rel, name) if x)


def _meta_bounds(out_dir: Path, *, tile_dir: Path | None = None,
                 tileset: Path | None = None, layer_json: Path | None = None,
                 vector: Path | None = None) -> tuple[list[float] | None, bool]:
    """按优先级取范围（design.md §3.1.1）。返回 (范围, 是否估算值)。"""
    b = bounds_from_metadata(out_dir)
    if b:
        return b, False
    if tile_dir is not None:
        got = bounds_from_tilemapresource(tile_dir)
        if got:
            return got[0], got[1]
    if tileset is not None:
        b = bounds_from_tileset(tileset)
        if b:
            return b, False
    if layer_json is not None:
        b = bounds_from_layer_json(layer_json)
        if b:
            return b, True
    if vector is not None:
        b = bounds_for_vector(vector)
        if b:
            return b, False
    if tile_dir is not None:
        grid = detect_grid(tile_dir)
        b = bounds_from_tile_index(tile_dir, grid)
        if b:
            return list(b), True
    return None, False


class _Tree:
    """一次遍历目录树，缓存"目录 -> 直接子项"与"目录 -> 是否含瓦片文件"。

    **为什么要缓存**：早先的实现对每个目录各做一次 rglob("*")，而 _walk 又会
    递归进子目录再扫一遍，等于对同一棵子树反复遍历。实测 output/ 下两个 3D
    Tiles 目录各有 3.8 万个文件，整个 output 有 9.2 万个文件，平方级的重复
    遍历会让扫描超过两分钟。现在只走一次盘，之后全部在内存里判定。
    """

    def __init__(self, root: Path, max_depth: int):
        self.root = root
        self.children: dict[Path, list[Path]] = {}
        self.dirs: dict[Path, list[Path]] = {}
        self.has_tile: dict[Path, bool] = {}
        self.tile_ext: dict[Path, str] = {}
        self._roots: dict[Path, tuple[int, int]] | None = None
        self._scan(root, 0, max_depth)

    def _scan(self, d: Path, depth: int, max_depth: int) -> None:
        files: list[Path] = []
        dirs: list[Path] = []
        try:
            with os.scandir(d) as it:
                for e in it:
                    p = Path(e.path)
                    if e.is_dir(follow_symlinks=False):
                        dirs.append(p)
                    elif e.is_file(follow_symlinks=False):
                        files.append(p)
        except OSError as ex:
            logger.debug("读取目录失败 %s:%s", d, ex)
        self.children[d] = files
        self.dirs[d] = dirs
        self.has_tile[d] = any(
            f.suffix.lower() in _TILE_SUFFIXES for f in files)
        for f in files:
            if f.suffix.lower() in _TILE_SUFFIXES:
                self.tile_ext[d] = f.suffix.lower().lstrip(".")
                break
        if depth >= max_depth:
            return
        for sub in dirs:
            if sub.name.startswith((".", "_")):
                continue
            self._scan(sub, depth + 1, max_depth)

    def tile_levels(self, d: Path) -> list[int]:
        """数字级别子目录（如 0/ 1/ 12/），升序。只需查缓存。"""
        return sorted(int(p.name) for p in self.dirs.get(d, [])
                      if p.name.isdigit())

    def any_tiles_below(self, d: Path) -> bool:
        """d 及其后代是否有瓦片文件。"""
        if self.has_tile.get(d):
            return True
        return any(self.any_tiles_below(s) for s in self.dirs.get(d, []))

    def ext_below(self, d: Path) -> str:
        """d 及其后代第一个瓦片文件的扩展名。"""
        if self.tile_ext.get(d):
            return self.tile_ext[d]
        for s in self.dirs.get(d, []):
            e = self.ext_below(s)
            if e:
                return e
        return "png"

    def tile_roots(self) -> dict[Path, tuple[int, int]]:
        """从**瓦片文件反推**瓦片根，返回 {根目录: (最小级别, 最大级别)}。

        结果缓存——_scan_one_dir 会对每个目录查一次，重算会是平方级的。

        为什么反推而不是从目录往下猜：`{z}/{x}/{y}.png` 与 `{z}/{col}_{row}.ext`
        两种命名的层级数不同，仅凭"某目录下方有瓦片"无法区分瓦片根与级别目录
        （tms 与 tms/3 都满足），会重复识别。

        `col_row` 是**文件名**不是目录名，所以两种形态下"含有瓦片文件的目录"
        到级别目录的距离不同：

          {root}/{z}/{x}/{y}.png      瓦片文件在 {x} 目录里，其父目录是级别
          {root}/{z}/{col}_{row}.ext  瓦片文件直接在级别目录里

        判定：若该目录名是纯数字且其父目录名也是纯数字，说明它是 {x} 目录，
        级别目录是它的父目录；若该目录名是纯数字且父目录不是数字，它自己就是
        级别目录（对应 col_row 扁平命名）。
        """
        if self._roots is not None:
            return self._roots
        roots: dict[Path, tuple[int, int]] = {}
        for d, files in self.children.items():
            if not any(f.suffix.lower() in _TILE_SUFFIXES for f in files):
                continue
            if not d.name.isdigit():
                continue
            lv = d.parent if d.parent.name.isdigit() else d
            root = lv.parent
            if not (root == self.root or self.root in root.parents):
                continue
            z = int(lv.name)
            lo, hi = roots.get(root, (z, z))
            roots[root] = (min(lo, z), max(hi, z))
        self._roots = roots
        return roots


def _scan_one_dir(d: Path, rel: str, tree: _Tree) -> list[Candidate]:
    """识别单个目录下可能存在的成果。rel 是相对扫描根的路径，用于显示名。"""
    out: list[Candidate] = []
    out_dir = d

    def _prefix(sub: Path) -> str:
        return sub.relative_to(d).as_posix() if sub != d else ""

    # ---- 模型：目录内有 tileset.json ----
    for f in tree.children.get(d, []):
        if f.name != "tileset.json":
            continue
        if (d.parent.name == "Data" and d.name.startswith("Tile_")
                and (d.parent.parent / "tileset.json").is_file()
                and d != tree.root):
            continue
        b, approx = _meta_bounds(out_dir, tileset=f)
        out.append(Candidate(
            kind=KIND_MODEL, root=str(d.resolve()),
            entry="tileset.json", label=_join(rel, _prefix(d)),
            bounds_wgs84=b, bounds_approx=approx))

    # ---- 地形：目录含 layer.json ----
    for f in tree.children.get(d, []):
        if f.name != "layer.json":
            continue
        levels = tree.tile_levels(d)
        b, approx = _meta_bounds(out_dir, layer_json=f)
        out.append(Candidate(
            kind=KIND_TERRAIN, root=str(d.resolve()), entry="",
            label=_join(rel, _prefix(d)),
            minzoom=levels[0] if levels else 0,
            maxzoom=levels[-1] if levels else 18,
            bounds_wgs84=b, bounds_approx=approx))

    # ---- 影像：本目录是瓦片根 ----
    # 判据来自 **从瓦片文件反推的根集合**（tree.tile_roots），而不是"下方有
    # 没有瓦片"——后者对 tms 与 tms/3 都成立，会把同一份数据识别两次。
    rng = tree.tile_roots().get(d)
    if rng is not None:
        grid = detect_grid(d)
        b, approx = _meta_bounds(out_dir, tile_dir=d)
        out.append(Candidate(
            kind=KIND_IMAGERY, root=str(d.resolve()), entry="",
            label=_join(rel, _prefix(d)), grid=grid,
            flip_y=(grid == "geodetic"),
            minzoom=rng[0], maxzoom=rng[1],
            bounds_wgs84=b, bounds_approx=approx,
            tile_ext=tree.ext_below(d)))

    # ---- 矢量：本目录下的单个文件 ----
    for p in tree.children.get(d, []):
        if p.suffix.lower() not in _VECTOR_SUFFIXES:
            continue
        if p.name.startswith(("_", ".")):
            continue
        b, approx = _meta_bounds(out_dir, vector=p)
        out.append(Candidate(
            kind=KIND_VECTOR, root=str(d.resolve()), entry=p.name,
            label=_join(rel, p.name), bounds_wgs84=b, bounds_approx=approx))

    return out


def scan_dir(path: Path, *, max_depth: int = MAX_DEPTH) -> list[Candidate]:
    """扫描目录（含最多 max_depth 层子目录），产出成果候选。

    返回的每个候选的 root 是**可直接做服务根**的绝对目录；entry 是相对 root
    的入口。同一种成果不会重复——比如 tms 目录只会命中一次影像。

    实现要点：**只走一次盘**（_Tree 缓存），之后所有判定在内存里完成。
    见 _Tree 的说明：重复遍历 9 万文件的目录会让扫描慢到不可用。
    """
    if not path.is_dir():
        return []
    root = path.resolve()
    tree = _Tree(root, max_depth)
    seen: set[tuple[str, str]] = set()
    out: list[Candidate] = []

    def _walk(d: Path, rel: str, depth: int):
        if depth > max_depth:
            return
        try:
            found = _scan_one_dir(d, rel, tree)
        except OSError as e:
            logger.debug("扫描目录失败 %s:%s", d, e)
            found = []
        for c in found:
            key = (c.kind, c.root)
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        if depth == max_depth:
            return
        for sub in tree.dirs.get(d, []):
            if sub.name.startswith((".", "_")):
                continue
            _walk(sub, _join(rel, sub.name), depth + 1)

    _walk(root, "", 0)
    return out
