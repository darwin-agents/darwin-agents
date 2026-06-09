"""Throttle routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Throttle' tag instead of the catch-all default.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)

from ..runtime.supervisor import SupervisorError, UnknownDarwin
from ..security.auth import (
    PLATFORM_OPERATOR,
    AuthContext,
)

from .deps import (
    _actor,
    _require_scope,
    _sup,
    get_auth,
)
from .models import (
    ResourceThrottleRequest,
    ThrottleRequest,
)

router = APIRouter(tags=["Throttle"])


# -- non-LLM resource throttles (doc 09 level 2) -----------------------
@router.post("/platform/darwins/{darwin_id}/resource_throttle")
async def set_resource_throttle(
    darwin_id: str,
    request: Request,
    req: ResourceThrottleRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        t = _sup(request).set_resource_throttle(
            darwin_id,
            kind=req.kind,
            max_daily_calls=req.max_daily_calls,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(422, str(exc))
    return t.to_json()


@router.delete("/platform/darwins/{darwin_id}/resource_throttle")
async def clear_resource_throttle(
    darwin_id: str,
    request: Request,
    kind: str | None = Query(None, pattern=r"^(sandbox|browser|egress)$"),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        removed = _sup(request).clear_resource_throttle(
            darwin_id,
            kind=kind,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {"removed": [r.to_json() for r in removed]}


@router.get("/platform/darwins/{darwin_id}/resource_throttle")
async def get_resource_throttle(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        rows = _sup(request).list_resource_throttles(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {"throttles": [r.to_json() for r in rows]}


# -- internal: throttle (doc 09 level 2) ------------------------------
@router.post("/platform/darwins/{darwin_id}/throttle")
async def install_throttle(
    darwin_id: str,
    request: Request,
    req: ThrottleRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).throttle_llm_tier(
            darwin_id,
            tier=req.tier,
            max_daily_calls=req.max_daily_calls,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(400, str(exc))


@router.delete("/platform/darwins/{darwin_id}/throttle")
async def clear_throttle(
    darwin_id: str,
    request: Request,
    tier: str | None = Query(None),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).clear_llm_throttle(darwin_id, tier=tier, actor=_actor(ctx))
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(400, str(exc))


@router.get("/platform/darwins/{darwin_id}/throttle")
async def get_throttles(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return {"throttles": await _sup(request).get_llm_throttles(darwin_id)}
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
