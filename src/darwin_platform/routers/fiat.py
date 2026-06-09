"""Fiat routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Fiat' tag instead of the catch-all default.
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
    DarwinFiatPayoutRequest,
    DarwinFiatRefundRequest,
    FiatConfirmDepositRequest,
    FiatDepositQuoteRequest,
    FiatWithdrawRequest,
)

router = APIRouter(tags=["Fiat"])


# -- fiat ramp (doc 17) -------------------------------------------------
@router.post("/platform/fiat/deposit_quote")
async def fiat_deposit_quote(
    request: Request,
    req: FiatDepositQuoteRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    if req.destination_darwin_id is not None:
        _require_owns(ctx, req.destination_darwin_id)
    try:
        quote = _sup(request).fiat_deposit_quote(
            amount_usd=req.amount_usd,
            payment_method=req.payment_method,
            destination_darwin_id=req.destination_darwin_id,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    return quote.to_json()


@router.post("/platform/fiat/confirm_deposit")
async def fiat_confirm_deposit(
    request: Request,
    req: FiatConfirmDepositRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    if req.destination_darwin_id is not None:
        _require_owns(ctx, req.destination_darwin_id)
    try:
        receipt = _sup(request).fiat_confirm_deposit(
            quote_id=req.quote_id,
            destination_darwin_id=req.destination_darwin_id,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    return receipt.to_json()


@router.post("/platform/fiat/withdraw")
async def fiat_withdraw(
    request: Request,
    req: FiatWithdrawRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        receipt = _sup(request).fiat_withdraw(
            amount_usdc=req.amount_usdc,
            destination=req.destination,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    return receipt.to_json()


# -- Darwin → fiat payout (doc 17 §"Darwin paying a human") ------------
@router.post("/darwins/{darwin_id}/fiat_payment")
async def darwin_fiat_payout(
    darwin_id: str,
    request: Request,
    req: DarwinFiatPayoutRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Two-leg payout: Darwin USDC wallet → omnibus address (chain) →
    fiat destination (Stripe Connect / MoonPay). Operator-initiated
    after the genome's facilitator escalation has been owner-approved."""
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).darwin_fiat_payout(
            darwin_id,
            amount_usdc=req.amount_usdc,
            freelancer_id=req.freelancer_id,
            escalation_id=req.escalation_id,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.post("/darwins/{darwin_id}/fiat_payment/refund")
async def darwin_fiat_refund(
    darwin_id: str,
    request: Request,
    req: DarwinFiatRefundRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Recovery for a failed fiat payout: refund the leg-1 USDC
    from the omnibus address back to the Darwin's wallet."""
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return await _sup(request).refund_fiat_payout(
            darwin_id,
            amount_usdc=req.amount_usdc,
            reason=req.reason,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
