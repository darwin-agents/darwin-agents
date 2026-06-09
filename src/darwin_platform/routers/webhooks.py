"""Webhooks routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Webhooks' tag instead of the catch-all default.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
)

from ..runtime.supervisor import SupervisorError

from .deps import (
    _sup,
)

router = APIRouter(tags=["Webhooks"])


@router.post("/webhooks/stripe")
async def stripe_webhook(request: Request) -> dict[str, Any]:
    """Stripe deposit webhook. UNAUTHENTICATED at the bearer layer —
    authenticity is established by verifying Stripe's signature against
    the configured signing secret (set via --stripe-webhook-secret).
    On ``payment_intent.succeeded`` the matching deposit is confirmed
    and the destination Darwin's wallet credited. Always returns 200 on
    a verified event (even when skipped) so Stripe stops retrying; a
    bad/again signature is a 400."""
    secret = getattr(request.app.state, "stripe_webhook_secret", None)
    if not secret:
        raise HTTPException(503, "stripe webhook secret not configured")
    try:
        import stripe
    except ImportError:
        raise HTTPException(503, "stripe SDK not installed")
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = stripe.Webhook.construct_event(payload, sig, secret)
    except Exception as exc:  # SignatureVerificationError / ValueError
        raise HTTPException(400, f"invalid stripe signature: {exc}")
    # construct_event returns a Stripe object; normalize to a plain dict.
    if not isinstance(event, dict):
        event = json.loads(payload.decode("utf-8") or "{}")
    return _sup(request).handle_stripe_webhook(event)


@router.post("/webhooks/moonpay")
async def moonpay_webhook(request: Request) -> dict[str, Any]:
    """MoonPay deposit/withdraw webhook. UNAUTHENTICATED at the bearer
    layer — authenticity is established by the adapter verifying the
    ``Moonpay-Signature-V2`` header against the configured webhook key.
    A ``completed`` buy credits the destination Darwin's wallet (routed
    by the transaction's ``externalCustomerId``). A verified event always
    returns 200 (even when skipped/ignored) so MoonPay stops retrying; a
    bad/missing signature is a 400; an unconfigured ramp is a 503."""
    from ..adapters.fiat import FiatRampError

    payload = await request.body()
    sig = request.headers.get("Moonpay-Signature-V2", "")
    try:
        return _sup(request).handle_moonpay_webhook(payload, sig)
    except FiatRampError as exc:
        raise HTTPException(400, f"invalid moonpay signature: {exc}")
    except SupervisorError as exc:
        raise HTTPException(503, str(exc))


@router.post("/webhooks/kyc")
async def kyc_provider_webhook(request: Request) -> dict[str, Any]:
    """KYC provider callback (Persona / Onfido). UNAUTHENTICATED at the
    bearer layer — authenticity is the provider's signature over the raw
    body (``Persona-Signature`` / ``X-SHA2-Signature``), verified by the
    configured KYC adapter. A verified event refreshes + caches the
    subject's verdict and returns 200; a bad/missing signature is a 400;
    an unconfigured KYC service is a 503.

    This is the provider-driven path; the operator/manual poll trigger
    lives at ``POST /platform/kyc/webhook`` (bearer-authenticated)."""
    from ..security.kyc import KYCError

    payload = await request.body()
    try:
        return _sup(request).handle_kyc_webhook(payload, dict(request.headers))
    except KYCError as exc:
        raise HTTPException(400, f"invalid kyc webhook: {exc}")
    except SupervisorError as exc:
        raise HTTPException(503, str(exc))
