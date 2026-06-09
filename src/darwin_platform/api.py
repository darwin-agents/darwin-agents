"""The Darwin Supervisor's HTTP surface (docs/engineering/08).

Maps doc-08's endpoints onto :class:`darwin_platform.Supervisor`. The class
holds all behaviour; this file is the thin REST wiring. ``fastapi`` is an
*optional* dependency (``pip install darwin-dna[platform]``) — importing this
module without it raises a clear error rather than failing at request time.

Run for development::

    uvicorn darwin_platform.api:app --reload

In production this would sit behind the platform's edge (Cloudflare/Fastly)
with auth in front; for this first pass authentication is out of scope and the
service trusts every caller. Do not expose this surface publicly as-is.
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager


try:  # pragma: no cover - import-time guard
    from fastapi import FastAPI
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "darwin_platform.api requires the [platform] extra: `pip install darwin-dna[platform]`"
    ) from exc

from .runtime.supervisor import Supervisor
from .security.auth import (
    TokenStore,
)


# Backward-compatible re-exports: the request models and the Prometheus
# helpers used to be defined in this module. They now live in the routers
# package; re-exported here so existing imports keep working.
from .routers import ALL_ROUTERS
from .routers._openapi import DESCRIPTION as _OPENAPI_DESCRIPTION
from .routers._openapi import TAGS as _OPENAPI_TAGS
from .routers._openapi import apply_openapi_metadata
from .routers.metrics_text import _escape_label, _fmt, _render_metrics
from .routers.models import (
    AmendmentRequest,
    BlockCounterpartyRequest,
    DarwinFiatPayoutRequest,
    DarwinFiatRefundRequest,
    DisabledCategoriesRequest,
    DisabledNeuronsRequest,
    EscalationResponseRequest,
    FiatConfirmDepositRequest,
    FiatDepositQuoteRequest,
    FiatWithdrawRequest,
    FleetBlockCategoryRequest,
    FleetSpendLockRequest,
    HarvestRequest,
    HarvestRuleRequest,
    KillRequest,
    KYCStartRequest,
    KYCWebhookRequest,
    OmnibusCreditRequest,
    PauseRequest,
    PreApprovalRequest,
    PushSubscriptionRequest,
    QuarantineRequest,
    RegisterFreelancerRequest,
    RejectProposalRequest,
    ReproductionBlockRequest,
    ResourceThrottleRequest,
    RetryOmnibusFundRequest,
    RotateSigningKeyRequest,
    SpawnRequest,
    SpendLockRequest,
    ThrottleRequest,
    VaultPutRequest,
    WalletPolicyRequest,
)

__all__ = [
    "build_app",
    "app",
    "_render_metrics",
    "_escape_label",
    "_fmt",
    "AmendmentRequest",
    "BlockCounterpartyRequest",
    "DarwinFiatPayoutRequest",
    "DarwinFiatRefundRequest",
    "DisabledCategoriesRequest",
    "DisabledNeuronsRequest",
    "EscalationResponseRequest",
    "FiatConfirmDepositRequest",
    "FiatDepositQuoteRequest",
    "FiatWithdrawRequest",
    "FleetBlockCategoryRequest",
    "FleetSpendLockRequest",
    "HarvestRequest",
    "HarvestRuleRequest",
    "KillRequest",
    "KYCStartRequest",
    "KYCWebhookRequest",
    "OmnibusCreditRequest",
    "PauseRequest",
    "PreApprovalRequest",
    "PushSubscriptionRequest",
    "QuarantineRequest",
    "RegisterFreelancerRequest",
    "RejectProposalRequest",
    "ReproductionBlockRequest",
    "ResourceThrottleRequest",
    "RetryOmnibusFundRequest",
    "RotateSigningKeyRequest",
    "SpawnRequest",
    "SpendLockRequest",
    "ThrottleRequest",
    "VaultPutRequest",
    "WalletPolicyRequest",
]


# -- app factory ------------------------------------------------------------
def _env_flag(name: str) -> bool:
    """True when env var ``name`` is set to a truthy string (1/true/yes/on)."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def build_app(
    supervisor: Supervisor | None = None,
    *,
    token_store: TokenStore | None = None,
    cors_origins: list[str] | None = None,
    stripe_webhook_secret: str | None = None,
    allow_manual_credit: bool = False,
    require_spawn_consent: bool = False,
    enable_docs: bool | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to ``supervisor`` (or a fresh one).

    Auth (doc 08-adjacent): ``token_store`` enables bearer-token auth.
    When ``None``, every request gets a permissive
    :func:`disabled_context` — fine for in-process tests + localhost dev,
    unsafe for any real exposure. Real deployments construct an
    :class:`InMemoryTokenStore` (or a Postgres-backed equivalent) and pass
    it in.

    ``cors_origins`` opts the dashboard's separate-origin browser into the
    API. Pass the Next.js dev origin (``http://localhost:3000``) — when
    empty the app refuses cross-origin requests (preflight OPTIONS 405s),
    which is the safe production default.

    Tests use this directly with an ASGI client; the module-level ``app``
    below is the default singleton for the standard uvicorn entry point.
    """
    sup = supervisor or Supervisor()
    app_started_ts = time.time()

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # The supervisor is bound below so we can also support build_app()
        # being called with an explicit instance — keep this thin.
        # Start leader election on app startup so /readyz reports correctly
        # before the first probe and the drive loops only run if this pod
        # is the leader (HA Postgres deployments).
        await app_.state.supervisor.ensure_leader_started()
        try:
            yield
        finally:
            await app_.state.supervisor.shutdown()

    # Interactive API docs (Swagger UI / ReDoc / OpenAPI JSON) are off by
    # default — this surface is meant to sit behind the platform edge/auth
    # and should not be publicly reachable as-is. Opt in for local dev with
    # ``DARWIN_ENABLE_DOCS=1``. ``enable_docs`` lets tests/callers force it.
    docs_on = enable_docs if enable_docs is not None else _env_flag("DARWIN_ENABLE_DOCS")
    app = FastAPI(
        title="Darwin Supervisor",
        version="0.1.0",
        description=_OPENAPI_DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=_OPENAPI_TAGS,
        docs_url="/docs" if docs_on else None,
        redoc_url="/redoc" if docs_on else None,
        openapi_url="/openapi.json" if docs_on else None,
    )
    app.state.supervisor = sup
    app.state.token_store = token_store
    app.state.stripe_webhook_secret = stripe_webhook_secret
    app.state.allow_manual_credit = bool(allow_manual_credit)
    app.state.require_spawn_consent = bool(require_spawn_consent)

    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cors_origins),
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"],
            allow_headers=[
                "Authorization",
                "Content-Type",
                "Idempotency-Key",
                "X-Darwin-Client",
            ],
            expose_headers=["Content-Disposition"],
            max_age=600,
        )

    # Group every domain router under its OpenAPI tag and mount it.
    app.state.started_ts = app_started_ts
    for _router in ALL_ROUTERS:
        app.include_router(_router)

    # Stamp per-endpoint summaries/descriptions for Swagger.
    apply_openapi_metadata(app)

    return app


# Default singleton for ``uvicorn darwin_platform.api:app``.
app = build_app()
