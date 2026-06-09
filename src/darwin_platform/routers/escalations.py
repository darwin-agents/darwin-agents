"""Escalations routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Escalations' tag instead of the catch-all default.
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
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
)

from .deps import (
    _actor,
    _require_owns,
    _require_scope,
    _sup,
    get_auth,
)
from .models import (
    EscalationResponseRequest,
)

router = APIRouter(tags=["Escalations"])


# -- escalation response ----------------------------------------------
@router.post("/darwins/{darwin_id}/escalations/{escalation_id}/respond")
async def respond_escalation(
    darwin_id: str,
    escalation_id: str,
    req: EscalationResponseRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        await _sup(request).respond_escalation(
            darwin_id,
            escalation_id=escalation_id,
            decision=req.decision,
            data=req.data,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(400, str(exc))
    return {"status": "responded"}


@router.get("/escalations")
async def list_escalations_fleet(
    request: Request,
    include_resolved: bool = Query(False),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Owner-scoped fleet view. Platform operators see every Darwin;
    owners see only their owned set. Sorted by deadline (soonest first)."""
    _require_scope(ctx, OWNER)
    sup = _sup(request)
    if ctx.has_scope(PLATFORM_OPERATOR):
        scope_ids = None
    else:
        scope_ids = [d for d in sup.list_ids() if ctx.owns(d)]
    tickets = await sup.open_escalations_for_darwins(scope_ids, include_resolved=include_resolved)
    return {
        "include_resolved": include_resolved,
        "tickets": tickets,
    }


@router.get("/darwins/{darwin_id}/escalations")
async def list_escalations(
    darwin_id: str,
    request: Request,
    include_resolved: bool = Query(False),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        tickets = await _sup(request).open_escalations(darwin_id, include_resolved=include_resolved)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {
        "darwin_id": darwin_id,
        "include_resolved": include_resolved,
        "tickets": tickets,
    }


@router.get("/darwins/{darwin_id}/escalations/{escalation_id}")
async def get_escalation(
    darwin_id: str,
    escalation_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).get_escalation(darwin_id, escalation_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(404, str(exc))
