"""将内嵌的 OSGB Tile 子树拆成外部 3D Tiles 清单。"""
from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


@dataclass(frozen=True)
class OptimizeResult:
    external_count: int
    original_bytes: int
    optimized_bytes: int
    missing_count: int


def _tile_dir(uri: str) -> str | None:
    parts = PurePosixPath(uri.removeprefix("./")).parts
    if (len(parts) >= 3 and parts[0] == "Data"
            and parts[1].startswith("Tile_") and parts[-1].endswith(".b3dm")):
        return "/".join(parts[:2])
    return None


def _content_uris(node: dict):
    content = node.get("content") or {}
    if isinstance(content.get("uri"), str):
        yield content["uri"]
    for child in node.get("children", []):
        yield from _content_uris(child)


def _local_tile_dir(node: dict) -> str | None:
    dirs = {_tile_dir(uri) for uri in _content_uris(node)}
    if len(dirs) == 1 and None not in dirs:
        return dirs.pop()
    return None


def _rewrite_uris(node: dict, tile_dir: str) -> None:
    prefix = f"{tile_dir}/"
    content = node.get("content") or {}
    if "uri" in content:
        uri = content["uri"].removeprefix("./")
        if not uri.startswith(prefix):
            raise ValueError(f"瓦片引用不在预期目录中:{uri}")
        content["uri"] = f"./{uri[len(prefix):]}"
    for child in node.get("children", []):
        _rewrite_uris(child, tile_dir)


def _validate_uris(node: dict, base: Path) -> None:
    for uri in _content_uris(node):
        relative = PurePosixPath(uri.removeprefix("./"))
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"瓦片引用路径无效:{uri}")
        target = base.joinpath(*relative.parts)
        if not target.is_file():
            raise ValueError(f"瓦片引用不存在:{target}")


def _prune_missing(node: dict, base: Path, missing: list[str]) -> dict | None:
    children = [_prune_missing(child, base, missing)
                for child in node.get("children", [])]
    children = [child for child in children if child is not None]
    if children:
        node["children"] = children
    else:
        node.pop("children", None)
    content = node.get("content") or {}
    uri = content.get("uri")
    if uri and not (base / uri).is_file():
        missing.append(uri)
        node.pop("content", None)
    if "content" not in node and "children" not in node:
        return None
    return node


def _atomic_json(path: Path, data: dict) -> None:
    temp = path.with_name(path.name + ".tmp")
    try:
        temp.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")),
                        encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _scale_errors(node: dict, source_error: float, target_error: float) -> None:
    """保留 Tile 内部 LOD 的相对误差比例。"""
    old = float(node.get("geometricError", 0))
    node["geometricError"] = (max(0.0, old / source_error * target_error)
                              if source_error > 0 else 0.0)
    for child in node.get("children", []):
        _scale_errors(child, source_error, target_error)


def optimize_tileset(path: str | Path) -> OptimizeResult:
    """保留顶层金字塔，将各 Tile 子树外置，最后替换主清单。"""
    path = Path(path)
    original_bytes = path.stat().st_size
    # 整份清单必须进内存(要递归遍历并改写整棵树),但用 json.load 直接从文件
    # 句柄解析 —— read_text + json.loads 会同时持有完整字符串与整棵 DOM 两份。
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    root_dir = path.parent
    plans: list[tuple[Path, Path]] = []
    missing: list[str] = []
    data["root"] = _prune_missing(data["root"], root_dir, missing)
    if data["root"] is None:
        raise ValueError("所有瓦片内容均不存在,无法优化")

    box = data["root"]["boundingVolume"]["box"]
    span_x, span_y = 2 * abs(float(box[3])), 2 * abs(float(box[7]))
    root_error = math.hypot(span_x, span_y) / 2
    overview_error = max(span_x, span_y) / 144
    if root_error <= 0 or overview_error <= 0:
        raise ValueError("根包围盒水平范围无效,无法计算层级误差")

    def visit(node: dict, parent_error: float, pyramid_error: float,
              *, is_root: bool = False) -> dict:
        tile_dir = None if is_root else _local_tile_dir(node)
        if tile_dir:
            target_error = min(parent_error, max(overview_error / 8, pyramid_error))
            external = json.loads(json.dumps(node))
            _scale_errors(external, float(node.get("geometricError", 0)), target_error)
            _rewrite_uris(external, tile_dir)
            external_path = root_dir / tile_dir / "tileset.json"
            # 校验通过就立刻落临时文件,plans 只留路径对(#35):原先要把每个
            # 外部子树的完整 dict 攒到「全部校验通过」才写,大范围成果的子树
            # 上千个,累积起来是几百 MB。改成临时文件后,失败也能靠清理回滚。
            _validate_uris(external, external_path.parent)
            tmp_path = external_path.with_name(external_path.name + ".tmp")
            tmp_path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path.write_text(
                json.dumps({"asset": data["asset"],
                            "geometricError": target_error,
                            "root": external},
                           ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8")
            plans.append((external_path, tmp_path))
            return {
                "boundingVolume": node["boundingVolume"],
                "geometricError": target_error,
                "refine": node.get("refine", "REPLACE"),
                "content": {"uri": f"./{tile_dir}/tileset.json"},
            }
        content_uri = (node.get("content") or {}).get("uri", "")
        if "/_pyramid/" in content_uri:
            node["geometricError"] = min(parent_error, pyramid_error)
            next_error = max(overview_error / 8, pyramid_error / 2)
        else:
            next_error = pyramid_error
        current_error = float(node.get("geometricError", parent_error))
        if "children" in node:
            node["children"] = [visit(child, current_error, next_error)
                                for child in node["children"]]
        return node

    data["root"]["geometricError"] = root_error
    data["geometricError"] = root_error
    try:
        data["root"] = visit(data["root"], root_error, overview_error, is_root=True)
    except Exception:
        for _external_path, tmp_path in plans:
            tmp_path.unlink(missing_ok=True)
        raise
    if not plans:
        if missing:
            _atomic_json(path, data)
        return OptimizeResult(0, original_bytes, path.stat().st_size, len(missing))

    # 外部清单全部成功落盘后一次性改名,主清单最后替换。
    for external_path, tmp_path in plans:
        os.replace(tmp_path, external_path)
    _atomic_json(path, data)
    return OptimizeResult(len(plans), original_bytes, path.stat().st_size,
                          len(missing))
