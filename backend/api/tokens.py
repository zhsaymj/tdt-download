"""天地图 tk 使用池管理 REST API。

本机自用、无鉴权(与项目现状一致)。列表接口对密钥做脱敏,不回传完整明文。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..core import token_pool as tp

router = APIRouter(prefix="/api/tokens", tags=["tokens"])


class TokenCreate(BaseModel):
    token: str = Field(..., description="天地图密钥明文")
    label: str = Field(default="", description="备注名")
    max_requests: int = Field(default=tp.DEFAULT_MAX_REQUESTS, ge=1, description="每日请求上限")


class TokenUpdate(BaseModel):
    token: str | None = None
    label: str | None = None
    max_requests: int | None = Field(default=None, ge=1)
    enabled: bool | None = None


class ReorderBody(BaseModel):
    ordered_ids: list[int] = Field(..., description="按目标顺序排列的 token id 列表")


def _mask(token: str) -> str:
    """密钥脱敏:保留首尾各 4 位,中间用星号。"""
    t = token or ""
    if len(t) <= 8:
        return (t[:2] + "***") if t else ""
    return f"{t[:4]}{'*' * 6}{t[-4:]}"


def _view(row: dict, runtime: dict) -> dict:
    """把库记录 + 运行态计数合并成前端展示视图(脱敏)。"""
    st = runtime.get(row["id"], {})
    return {
        "id": row["id"],
        "token_masked": _mask(row["token"]),
        "label": row.get("label", ""),
        "order_index": row.get("order_index", 0),
        "request_count": st.get("request_count", row.get("request_count", 0)),
        "max_requests": row.get("max_requests", tp.DEFAULT_MAX_REQUESTS),
        "enabled": bool(row.get("enabled", 1)),
        "is_current": st.get("is_current", False),
    }


def _list_view() -> list[dict]:
    runtime = {s["id"]: s for s in tp.token_pool.status()}
    return [_view(r, runtime) for r in tp.list_tokens()]


@router.get("")
async def api_list_tokens():
    return _list_view()


@router.post("")
async def api_add_token(body: TokenCreate):
    try:
        tp.add_token(body.token, body.label, body.max_requests)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        # UNIQUE 约束冲突等
        raise HTTPException(400, f"添加失败(密钥可能已存在):{e}")
    tp.token_pool.reload()
    return _list_view()


@router.put("/{token_id}")
async def api_update_token(token_id: int, body: TokenUpdate):
    if not tp.get_token(token_id):
        raise HTTPException(404, "密钥不存在")
    fields = body.model_dump(exclude_none=True)
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    if "token" in fields and not (fields["token"] or "").strip():
        raise HTTPException(400, "密钥不能为空")
    try:
        tp.update_token(token_id, **fields)
    except Exception as e:
        raise HTTPException(400, f"更新失败(密钥可能重复):{e}")
    tp.token_pool.reload()
    return _list_view()


@router.delete("/{token_id}")
async def api_delete_token(token_id: int):
    if not tp.get_token(token_id):
        raise HTTPException(404, "密钥不存在")
    tp.delete_token(token_id)
    tp.token_pool.reload()
    return _list_view()


@router.post("/reorder")
async def api_reorder_tokens(body: ReorderBody):
    tp.reorder_tokens(body.ordered_ids)
    tp.token_pool.reload()
    return _list_view()


@router.post("/{token_id}/reset")
async def api_reset_token(token_id: int):
    if not tp.get_token(token_id):
        raise HTTPException(404, "密钥不存在")
    tp.reset_token_count(token_id)
    tp.token_pool.reload()
    return _list_view()
