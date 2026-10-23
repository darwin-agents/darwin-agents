"""Harvest routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Harvest' tag instead of the catch-all default.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..runtime.supervisor import SupervisorError, UnknownDarwin
from ..security.auth import (
    OWNER,
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
    HarvestRequest,
    HarvestRuleRequest,
    PayoutAddressRequest,
)

router = APIRouter(tags=["Harvest"])


# -- billing (doc 08 §"Billing model") --------------------------------
@router.post("/darwins/{darwin_id}/harvest")
async def harvest(
    darwin_id: str,
    request: Request,
    req: HarvestRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).harvest(
            darwin_id,
            amount_usdc=req.amount_usdc,
            destination=req.destination,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.put("/darwins/{darwin_id}/payout-address")
async def set_payout_address(
    darwin_id: str,
    request: Request,
    req: PayoutAddressRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).set_payout_address(
            darwin_id, address=req.address, actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- harvest rules (doc 05: scheduled profit withdrawal) ---------------
@router.post("/darwins/{darwin_id}/harvest_rules", status_code=201)
async def add_harvest_rule(
    darwin_id: str,
    request: Request,
    req: HarvestRuleRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        rule = _sup(request).add_harvest_rule(
            darwin_id,
            destination=req.destination,
            trigger_balance_usdc=req.trigger_balance_usdc,
            amount_usdc=req.amount_usdc,
            interval_seconds=req.interval_seconds,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(422, str(exc))
    return rule.to_json()


@router.get("/darwins/{darwin_id}/harvest_rules")
async def list_harvest_rules(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        rules = _sup(request).list_harvest_rules(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {
        "darwin_id": darwin_id,
        "rules": [r.to_json() for r in rules],
    }


@router.delete("/darwins/{darwin_id}/harvest_rules/{rule_id}")
async def delete_harvest_rule(
    darwin_id: str,
    rule_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        rule = _sup(request).remove_harvest_rule(
            darwin_id,
            rule_id,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(404, str(exc))
    return rule.to_json()
