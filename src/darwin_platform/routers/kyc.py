"""KYC routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'KYC' tag instead of the catch-all default.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..security.auth import (
    OWNER,
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
    KYCStartRequest,
    KYCWebhookRequest,
)

router = APIRouter(tags=["KYC"])


# -- KYC (doc 05 / 22) -------------------------------------------------
def _subject(ctx: AuthContext, requested: str | None) -> str:
    """Owners act on their own identity only; operators may name a subject."""
    if requested and requested != _actor(ctx):
        if not ctx.has_scope(PLATFORM_OPERATOR):
            raise HTTPException(403, "only operators may act on another subject")
        return requested
    return _actor(ctx)


def _kyc(request: Request):
    svc = _sup(request).kyc_service
    if svc is None:
        raise HTTPException(503, "KYC is not configured")
    return svc


@router.post("/platform/kyc/start")
async def kyc_start(
    request: Request,
    req: KYCStartRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Begin a KYC inquiry for the caller (or, for operators, a named
    subject). Returns the provider inquiry id + redirect URL."""
    _require_scope(ctx, OWNER)
    from ..security.kyc import KYCError, KYCTier

    svc = _kyc(request)
    subject = _subject(ctx, req.subject)
    try:
        return svc.start_inquiry(
            subject=subject,
            tier=KYCTier(req.tier),
            actor=_actor(ctx),
        )
    except KYCError as exc:
        raise HTTPException(502, str(exc))


@router.get("/platform/kyc/status")
async def kyc_status(
    request: Request,
    subject: str | None = None,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Current cached KYC verdict for the subject (default: the caller)."""
    _require_scope(ctx, OWNER)
    svc = _kyc(request)
    return svc.status_for(_subject(ctx, subject)).to_json()


@router.post("/platform/kyc/webhook")
async def kyc_webhook(
    request: Request,
    req: KYCWebhookRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Provider callback / poll-consumption: refresh + cache the verdict
    for an inquiry so the gate sees the latest status."""
    _require_scope(ctx, OWNER)
    from ..security.kyc import KYCError

    svc = _kyc(request)
    try:
        verdict = svc.refresh_status(
            subject=_subject(ctx, req.subject),
            inquiry_id=req.inquiry_id,
            actor=_actor(ctx),
        )
    except KYCError as exc:
        raise HTTPException(403, str(exc))
    return verdict.to_json()
