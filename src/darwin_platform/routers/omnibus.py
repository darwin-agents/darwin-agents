"""Omnibus routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Omnibus' tag instead of the catch-all default.
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
    OmnibusCreditRequest,
    RetryOmnibusFundRequest,
)

router = APIRouter(tags=["Omnibus"])


@router.post("/darwins/{darwin_id}/omnibus/fund")
async def retry_omnibus_fund(
    darwin_id: str,
    request: Request,
    req: RetryOmnibusFundRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Retry a fund_child that was skipped at spawn time."""
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).retry_omnibus_fund(
            darwin_id,
            amount_usdc=req.amount_usdc,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.post("/platform/omnibus/credit")
async def omnibus_credit(
    request: Request,
    req: OmnibusCreditRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Credit a user's omnibus balance for an already-settled deposit.
    Operator-scoped AND off by default — it mints credit not tied to a
    verified on-chain/Stripe deposit, so it's a faucet/testnet affordance
    only. Enable explicitly with --allow-manual-credit; production
    (real-money) deploys should leave it disabled and credit solely via
    the Stripe webhook + on-chain deposits."""
    if not getattr(request.app.state, "allow_manual_credit", False):
        raise HTTPException(
            403,
            "manual omnibus credit is disabled; boot with "
            "--allow-manual-credit to enable (testnet/dev only)",
        )
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return _sup(request).credit_user_omnibus(
            user_subject=req.user_subject,
            amount_usdc=req.amount_usdc,
            source_ref=req.source_ref or "manual:operator",
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
