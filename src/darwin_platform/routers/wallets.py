"""Wallets routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Wallets' tag instead of the catch-all default.
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
    PreApprovalRequest,
    WalletPolicyRequest,
)

router = APIRouter(tags=["Wallets"])


# -- internal: wallet policy (doc 08 POST /platform/wallets/.../policy)
@router.post("/platform/wallets/{darwin_id}/policy")
async def set_wallet_policy(
    darwin_id: str,
    request: Request,
    req: WalletPolicyRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        kwargs = {
            "per_action_cap_usdc": req.per_action_cap_usdc,
            "per_day_cap_usdc": req.per_day_cap_usdc,
            "actor": _actor(ctx),
        }
        # per_lifetime_cap_usdc is added in the slice-2 threading; the
        # request model carries it, but pass through only when set so
        # the Supervisor signature doesn't have to know yet.
        if getattr(req, "per_lifetime_cap_usdc", None) is not None:
            kwargs["per_lifetime_cap_usdc"] = req.per_lifetime_cap_usdc
        return await _sup(request).set_wallet_policy(darwin_id, **kwargs)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/platform/wallets/{darwin_id}/policy")
async def get_wallet_policy(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).get_wallet_policy(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- pre-approvals (doc 17 §"Darwin paying a human") -------------------
@router.put("/platform/wallets/{darwin_id}/preapprovals/{rule_id}")
async def set_preapproval(
    darwin_id: str,
    rule_id: str,
    request: Request,
    req: PreApprovalRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).set_preapproval(
            darwin_id,
            rule_id=rule_id,
            max_amount_usdc=req.max_amount_usdc,
            counterparty_category=req.counterparty_category,
            counterparty_substring=req.counterparty_substring,
            expires_at=req.expires_at,
            max_uses=req.max_uses,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.delete("/platform/wallets/{darwin_id}/preapprovals/{rule_id}")
async def revoke_preapproval(
    darwin_id: str,
    rule_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).revoke_preapproval(
            darwin_id,
            rule_id,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/platform/wallets/{darwin_id}/preapprovals")
async def list_preapprovals(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).list_preapprovals(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
