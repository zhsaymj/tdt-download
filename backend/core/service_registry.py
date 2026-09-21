"""本地数据服务的注册表：CRUD、路径穿越校验、健康探测。

**服务是指向，不是拥有。** 移除服务只删注册记录，绝不动磁盘上的文件。任务被
purge 删除时目录消失，对应服务变为"失效"而不自动删除——自动删会让"目录临时
不可用（外接盘没挂上）"变成永久丢失配置。

**新增服务默认关闭**：登记不等于对外提供，要显式开启。这样误登记不会立刻
把本机目录暴露出去。
"""
from __future__ import annotations

import json
import secrets
from pathlib import Path

from ..db import get_conn
from ..models import _now, _service_row_to_dict
from .logs import logger


def new_id() -> str:
    """8 位十六进制短 id，用作 URL 段。

    碰撞概率极低（4 字节），仍做一次存在性检查后返回——URL 里的 id 是服务的
    唯一标识，撞了会让两个服务共用一个地址。
    """
    for _ in range(8):
        cand = secrets.token_hex(4)
        with get_conn() as conn:
            hit = conn.execute(
                "SELECT 1 FROM services WHERE id = ?", (cand,)).fetchone()
        if not hit:
            return cand
    raise RuntimeError("无法生成唯一的服务 id")


def check_path(root: Path, rel: str) -> Path:
    """把 rel 拼到 root 之后并校验仍在 root 内。越界抛 PermissionError。

    这是文件服务的**唯一**安全闸门：`..` 穿越、绝对路径注入、符号链接逃逸
    全在这一处拦。root 必须是注册时已 resolve() 的绝对路径。

    注意 rel 可能是空的（服务根），也可能含多级（3/2/1.png）。用 resolve()
    归一化后再比对，短路径（8.3 格式）、大小写变体、符号链接都会被解开。
    """
    root = Path(root)
    candidate = (root / rel).resolve() if rel else root.resolve()
    if not candidate.is_relative_to(root):
        raise PermissionError(f"路径越界：{rel}")
    return candidate


def add_service(*, name: str, kind: str, root: str, entry: str = "",
                grid: str = "", flip_y: bool = False, minzoom: int = 0,
                maxzoom: int = 18, bounds_wgs84: list[float] | None = None,
                bounds_approx: bool = False, tile_ext: str = "png",
                source: str = "manual", enabled: bool = False) -> str:
    """登记一个服务，返回新 id。**默认关闭**——不主动对外暴露。"""
    sid = new_id()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO services (id, name, kind, root, entry, grid, flip_y,"
            " minzoom, maxzoom, bounds_wgs84, bounds_approx, tile_ext,"
            " enabled, source, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, name, kind, str(Path(root).resolve()), entry, grid,
             1 if flip_y else 0, int(minzoom), int(maxzoom),
             json.dumps(bounds_wgs84) if bounds_wgs84 else "",
             1 if bounds_approx else 0, tile_ext,
             1 if enabled else 0, source, _now()))
    logger.info("登记服务 %s(%s):%s", name, kind, root)
    return sid


def list_services() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM services ORDER BY created_at DESC").fetchall()
    return [_service_row_to_dict(r) for r in rows]


def get_service(sid: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM services WHERE id = ?", (sid,)).fetchone()
    return _service_row_to_dict(row) if row else None


def update_service(sid: str, **fields) -> bool:
    """改服务字段。**只接受白名单内的列名**，避免 SQL 注入。"""
    allowed = {"name", "enabled", "kind", "root", "entry", "grid", "flip_y",
               "minzoom", "maxzoom", "bounds_wgs84", "bounds_approx",
               "tile_ext", "source"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("enabled", "flip_y", "bounds_approx"):
            v = 1 if v else 0
        elif k == "bounds_wgs84":
            v = json.dumps(v) if v else ""
        sets.append(f"{k} = ?")
        vals.append(v)
    if not sets:
        return False
    vals.append(sid)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE services SET {', '.join(sets)} WHERE id = ?", vals)
    return cur.rowcount > 0


def remove_service(sid: str) -> bool:
    """移除注册记录。**不删磁盘文件**——服务是"指向"，不是"拥有"。"""
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM services WHERE id = ?", (sid,))
    if cur.rowcount:
        logger.info("移除服务 %s", sid)
    return cur.rowcount > 0


def service_health(sid: str) -> dict:
    """探测服务源是否还在。纯本地 exists()，毫秒级，不做常驻轮询。"""
    svc = get_service(sid)
    if svc is None:
        return {"ok": False, "reason": "服务不存在"}
    root = Path(svc["root"])
    if not root.is_dir():
        return {"ok": False, "reason": "源目录已不存在"}
    entry = svc.get("entry") or ""
    if entry:
        try:
            target = check_path(root, entry)
        except PermissionError:
            return {"ok": False, "reason": "入口路径越界"}
        if not target.is_file():
            return {"ok": False, "reason": f"入口文件已不存在：{entry}"}
    return {"ok": True, "reason": ""}


def health_all() -> dict[str, dict]:
    return {s["id"]: service_health(s["id"]) for s in list_services()}
