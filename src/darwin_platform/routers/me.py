"""Me routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Me' tag instead of the catch-all default.
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

from ..runtime.supervisor import SupervisorError
from ..security.auth import (
    OWNER,
    AuthContext,
)

from .deps import (
    _actor,
    _require_scope,
    _sup,
    get_auth,
)
from .models import (
    PushSubscriptionRequest,
    RegisterFreelancerRequest,
)

router = APIRouter(tags=["Me"])


# -- /me/freelancers — owner self-serve (doc 17) -----------------------
# Same store, but the owner_subject is implicit from ctx.subject so
# the owner cannot reach another user's destinations. Requires OWNER
# scope, which is satisfied by both PLATFORM_OPERATOR and a normal
# owner token (AuthContext.has_scope).
@router.post("/me/freelancers")
async def me_register_freelancer(
    request: Request,
    req: RegisterFreelancerRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    # An owner can only register under their own subject — body
    # owner_subject is ignored / overridden.
    subject = ctx.subject or "anonymous"
    try:
        return _sup(request).register_freelancer(
            owner_subject=subject,
            freelancer_id=req.freelancer_id,
            fiat_destination=req.fiat_destination,
            display_name=req.display_name,
            kyc_status=req.kyc_status,
            metadata=req.metadata,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/me/freelancers")
async def me_list_freelancers(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    _require_scope(ctx, OWNER)
    subject = ctx.subject or "anonymous"
    return _sup(request).list_freelancers(owner_subject=subject)


@router.delete("/me/freelancers/{freelancer_id}")
async def me_revoke_freelancer(
    freelancer_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    subject = ctx.subject or "anonymous"
    try:
        return _sup(request).revoke_freelancer(
            owner_subject=subject,
            freelancer_id=freelancer_id,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- /me/push-subscriptions — Web Push for the PWA (doc 22) ------------
# Owner self-serve only; an owner cannot reach another user's devices.
# The endpoint is sensitive (anyone with it + the VAPID key can deliver
# to that browser), so list responses redact it down to the origin and
# the audit log never sees the full URL.
@router.get("/me/push-vapid-public-key")
async def me_push_vapid_public_key(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    key = _sup(request).push_vapid_public_key()
    if not key:
        # 503 — the platform was started without Web Push configured.
        # 404 would imply "no such resource" which is misleading here.
        raise HTTPException(503, "web push is not configured")
    return {"public_key": key}


@router.post("/me/push-subscriptions", status_code=201)
async def me_register_push_subscription(
    request: Request,
    req: PushSubscriptionRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    subject = ctx.subject or "anonymous"
    try:
        return _sup(request).register_push_subscription(
            owner_subject=subject,
            endpoint=req.endpoint,
            p256dh=req.p256dh,
            auth=req.auth,
            user_agent=req.user_agent,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/me/push-subscriptions")
async def me_list_push_subscriptions(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    _require_scope(ctx, OWNER)
    subject = ctx.subject or "anonymous"
    return _sup(request).list_push_subscriptions(owner_subject=subject)


@router.delete("/me/push-subscriptions")
async def me_revoke_push_subscription(
    request: Request,
    endpoint: str = Query(..., min_length=1),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    # DELETE-with-body works across most servers but enough proxies
    # strip the body that a query param is more reliable. The endpoint
    # URL is bounded (browsers cap it at a few hundred chars) so the
    # ``?endpoint=…`` form fits comfortably.
    _require_scope(ctx, OWNER)
    subject = ctx.subject or "anonymous"
    try:
        return _sup(request).revoke_push_subscription(
            owner_subject=subject,
            endpoint=endpoint,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
