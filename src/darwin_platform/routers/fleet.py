"""Fleet routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Fleet' tag instead of the catch-all default.
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
    BlockCounterpartyRequest,
    FleetBlockCategoryRequest,
    FleetSpendLockRequest,
    QuarantineRequest,
)

router = APIRouter(tags=["Fleet"])


# -- fleet-wide incident-response levers (doc 09) ---------------------
@router.post("/platform/fleet/pause")
async def fleet_pause(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return await _sup(request).fleet_pause(actor=_actor(ctx))


@router.post("/platform/fleet/resume")
async def fleet_resume(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return await _sup(request).fleet_resume(actor=_actor(ctx))


@router.post("/platform/fleet/spend_lock")
async def fleet_spend_lock(
    request: Request,
    req: FleetSpendLockRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return await _sup(request).fleet_spend_lock(
        locked=req.locked,
        actor=_actor(ctx),
    )


@router.post("/platform/fleet/block_category")
async def fleet_block_category(
    request: Request,
    req: FleetBlockCategoryRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).fleet_block_category(
            category=req.category,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(400, str(exc))


@router.post("/platform/fleet/block_counterparty")
async def block_counterparty(
    request: Request,
    req: BlockCounterpartyRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return _sup(request).block_counterparty(
            req.address,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- internal: quarantine (doc 08 + doc 09 level 4) -------------------
@router.post("/platform/darwins/{darwin_id}/quarantine")
async def quarantine_darwin(
    darwin_id: str,
    request: Request,
    req: QuarantineRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        await _sup(request).quarantine(
            darwin_id,
            reason=req.reason,
            operator=req.operator,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "quarantined"}


@router.delete("/platform/darwins/{darwin_id}/quarantine")
async def unquarantine_darwin(
    darwin_id: str,
    request: Request,
    operator: str = Query("platform"),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        await _sup(request).unquarantine(darwin_id, operator=operator, actor=_actor(ctx))
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {"status": "cleared"}
