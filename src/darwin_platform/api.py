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

import base64
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

from fastapi import Response

try:  # pragma: no cover - import-time guard
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "darwin_platform.api requires the [platform] extra: "
        "`pip install darwin-dna[platform]`"
    ) from exc

from .observability.monitoring import Severity
from .observability.snapshot import snapshot as build_snapshot
from .runtime.supervisor import Supervisor, SupervisorError, UnknownDarwin
from .security.auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
    TokenStore,
    disabled_context,
)
from .tenancy.provisioning import ProvisioningError


# -- request/response models ------------------------------------------------
class SpawnRequest(BaseModel):
    opening_usdc: float = Field(default=100.0, gt=0)
    escalation_auto: str | None = Field(
        default="approve",
        description="Unattended owner-response policy for the fake inbox: "
        "'approve' | 'deny' | null. None means a real owner (or test) must "
        "respond via the respond_escalation endpoint.",
    )
    parent_id: str | None = None


class KillRequest(BaseModel):
    reason: str = "owner kill"


class PauseRequest(BaseModel):
    reason: str = "owner paused"


class EscalationResponseRequest(BaseModel):
    decision: str = Field(pattern=r"^(approve|deny|data)$")
    data: dict[str, Any] | None = None


class QuarantineRequest(BaseModel):
    reason: str
    operator: str = "platform"


class ResourceThrottleRequest(BaseModel):
    kind: str = Field(pattern=r"^(sandbox|browser|egress)$")
    max_daily_calls: int = Field(ge=0)


class FiatDepositQuoteRequest(BaseModel):
    amount_usd: float = Field(gt=0)
    payment_method: str = Field(min_length=1)


class FiatConfirmDepositRequest(BaseModel):
    quote_id: str = Field(min_length=1)
    destination_darwin_id: str | None = None


class FiatWithdrawRequest(BaseModel):
    amount_usdc: float = Field(gt=0)
    destination: str = Field(min_length=1)


class RotateSigningKeyRequest(BaseModel):
    drain_window_seconds: float | None = Field(default=None, gt=0)


class RejectProposalRequest(BaseModel):
    reason: str = "operator rejected"


class HarvestRuleRequest(BaseModel):
    destination: str = Field(min_length=1)
    trigger_balance_usdc: float = Field(gt=0)
    amount_usdc: float | None = Field(default=None, gt=0)
    interval_seconds: float | None = Field(default=None, ge=60)


class VaultPutRequest(BaseModel):
    tier: str = Field(pattern=r"^(public|scoped|sensitive)$")
    key: str = Field(min_length=1)
    value: str
    scope: str | None = None
    ttl_seconds: float | None = Field(default=None, gt=0)
    original_filename: str | None = None


class WalletPolicyRequest(BaseModel):
    per_action_cap_usdc: float | None = Field(default=None, ge=0)
    per_day_cap_usdc: float | None = Field(default=None, ge=0)
    per_lifetime_cap_usdc: float | None = Field(default=None, ge=0)


class ThrottleRequest(BaseModel):
    tier: str = Field(pattern=r"^(fast|reasoning|coding|image_gen|embedding)$")
    max_daily_calls: int = Field(ge=0)


class HarvestRequest(BaseModel):
    amount_usdc: float = Field(gt=0)
    destination: str = Field(min_length=1)


class AmendmentRequest(BaseModel):
    """A platform amendment layer to ship via forced reproduction (doc 02).

    ``amendment_blob_b64`` is the layer bytes base64-encoded so the JSON
    body is text-clean. ``amendment_name`` is the layer's filename inside
    the manifest (e.g. ``layer_5_amendment_kyc_required``). ``kill_parent_reason``
    annotates the kill audit entry that follows successful reproduction.
    """

    amendment_blob_b64: str
    amendment_name: str = Field(min_length=1)
    kill_parent_reason: str = "amendment reproduction"


# -- app factory ------------------------------------------------------------
def build_app(
    supervisor: Supervisor | None = None,
    *,
    token_store: TokenStore | None = None,
) -> FastAPI:
    """Build a FastAPI app bound to ``supervisor`` (or a fresh one).

    Auth (doc 08-adjacent): ``token_store`` enables bearer-token auth.
    When ``None``, every request gets a permissive
    :func:`disabled_context` — fine for in-process tests + localhost dev,
    unsafe for any real exposure. Real deployments construct an
    :class:`InMemoryTokenStore` (or a Postgres-backed equivalent) and pass
    it in.

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

    app = FastAPI(
        title="Darwin Supervisor",
        version="0.1.0",
        description="Platform control plane (docs/engineering/08), first pass.",
        lifespan=lifespan,
    )
    app.state.supervisor = sup
    app.state.token_store = token_store

    def _sup(request: Request) -> Supervisor:
        return request.app.state.supervisor

    # -- auth dependency --------------------------------------------------
    def get_auth(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> AuthContext:
        store: TokenStore | None = request.app.state.token_store
        if store is None:
            # No store configured → permissive mode (the existing in-process
            # tests rely on this). Documented in build_app's docstring.
            return disabled_context()
        if not authorization or not authorization.startswith("Bearer "):
            raise HTTPException(
                401,
                "missing bearer token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        token = authorization[len("Bearer ") :].strip()
        ctx = store.lookup(token)
        if ctx is None:
            raise HTTPException(
                401, "invalid token", headers={"WWW-Authenticate": "Bearer"}
            )
        return ctx

    def _require_scope(ctx: AuthContext, scope: str) -> None:
        if not ctx.has_scope(scope):
            raise HTTPException(403, f"requires scope: {scope}")

    def _require_owns(ctx: AuthContext, darwin_id: str) -> None:
        if not ctx.owns(darwin_id):
            raise HTTPException(403, f"forbidden: not your darwin: {darwin_id}")

    def _actor(ctx: AuthContext) -> str:
        """Operator identity for the audit log: prefer the human-readable
        ``subject`` (email / user id), fall back to the token, fall back to
        ``anonymous`` (auth-disabled mode)."""
        return ctx.subject or ctx.token or "anonymous"

    # -- spawn / status ----------------------------------------------------
    @app.post("/darwins", status_code=201)
    async def spawn(
        req: SpawnRequest,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        # Either an owner spawning their own Darwin or a platform operator
        # provisioning on behalf of one — either way the OWNER scope check
        # passes (operator implies owner).
        _require_scope(ctx, OWNER)
        try:
            status = await _sup(request).spawn(
                opening_usdc=req.opening_usdc,
                escalation_auto=req.escalation_auto,
                parent_id=req.parent_id,
                actor=_actor(ctx),
            )
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        # Grant the caller ownership of the newly minted id so subsequent
        # owner-scope calls succeed without an out-of-band grant.
        store: TokenStore | None = request.app.state.token_store
        if store is not None and ctx.token:
            store.grant(ctx.token, status.darwin_id)
        return asdict(status)

    @app.get("/darwins")
    async def list_darwins(
        request: Request, ctx: AuthContext = Depends(get_auth)
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        sup = _sup(request)
        # Platform operators see the whole fleet; owners see only their own.
        if ctx.has_scope(PLATFORM_OPERATOR):
            return {"darwins": sup.list_ids()}
        own = [d for d in sup.list_ids() if ctx.owns(d)]
        return {"darwins": own}

    @app.get("/darwins/{darwin_id}")
    async def status(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return asdict(await _sup(request).status(darwin_id))
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    # -- pause / resume / kill --------------------------------------------
    @app.post("/darwins/{darwin_id}/pause")
    async def pause(
        darwin_id: str,
        request: Request,
        req: PauseRequest = Body(default=PauseRequest()),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            await _sup(request).pause(
                darwin_id, reason=req.reason, actor=_actor(ctx)
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "pausing"}

    @app.post("/darwins/{darwin_id}/resume")
    async def resume(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            await _sup(request).resume(darwin_id, actor=_actor(ctx))
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "resumed"}

    @app.post("/darwins/{darwin_id}/kill")
    async def kill(
        darwin_id: str,
        request: Request,
        req: KillRequest = Body(default=KillRequest()),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            await _sup(request).kill(
                darwin_id, reason=req.reason, actor=_actor(ctx)
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"status": "killing"}

    # -- force reproduction -----------------------------------------------
    @app.post("/darwins/{darwin_id}/reproduce/force")
    async def reproduce_force(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            await _sup(request).force_reproduce(darwin_id, actor=_actor(ctx))
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "reproduction-requested"}

    # -- escalation response ----------------------------------------------
    @app.post("/darwins/{darwin_id}/escalations/{escalation_id}/respond")
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

    @app.get("/darwins/{darwin_id}/escalations")
    async def list_escalations(
        darwin_id: str,
        request: Request,
        include_resolved: bool = Query(False),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            tickets = await _sup(request).open_escalations(
                darwin_id, include_resolved=include_resolved
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {
            "darwin_id": darwin_id,
            "include_resolved": include_resolved,
            "tickets": tickets,
        }

    @app.get("/darwins/{darwin_id}/escalations/{escalation_id}")
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

    # -- lineage / ledger / events ----------------------------------------
    @app.get("/darwins/{darwin_id}/lineage")
    async def lineage(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return await _sup(request).lineage(darwin_id)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    @app.get("/darwins/{darwin_id}/lineage/tree")
    async def lineage_tree(
        darwin_id: str,
        request: Request,
        max_depth: int = Query(16, ge=1, le=128),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        """Phase 1 (doc 21 §"Lineage tree"): nested ancestors + descendants
        for the minimal dashboard. ``max_depth`` bounds the descendant
        recursion."""
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return await _sup(request).lineage_tree(
                darwin_id, max_depth=max_depth
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    @app.get("/darwins/{darwin_id}/ledger")
    async def ledger(
        darwin_id: str,
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return await _sup(request).ledger(darwin_id, limit=limit, offset=offset)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    @app.get("/darwins/{darwin_id}/events")
    async def events(
        darwin_id: str,
        request: Request,
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return await _sup(request).events(darwin_id, limit=limit, offset=offset)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    # -- billing (doc 08 §"Billing model") --------------------------------
    @app.post("/darwins/{darwin_id}/harvest")
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

    @app.get("/darwins/{darwin_id}/billing")
    async def darwin_billing(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return await _sup(request).billing(darwin_id)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    @app.get("/platform/billing")
    async def fleet_billing(
        request: Request, ctx: AuthContext = Depends(get_auth)
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        return await _sup(request).fleet_billing()

    # -- non-LLM resource throttles (doc 09 level 2) -----------------------
    @app.post("/platform/darwins/{darwin_id}/resource_throttle")
    async def set_resource_throttle(
        darwin_id: str,
        request: Request,
        req: ResourceThrottleRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            t = _sup(request).set_resource_throttle(
                darwin_id, kind=req.kind,
                max_daily_calls=req.max_daily_calls,
                actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(422, str(exc))
        return t.to_json()

    @app.delete("/platform/darwins/{darwin_id}/resource_throttle")
    async def clear_resource_throttle(
        darwin_id: str,
        request: Request,
        kind: str | None = Query(None, pattern=r"^(sandbox|browser|egress)$"),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            removed = _sup(request).clear_resource_throttle(
                darwin_id, kind=kind, actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"removed": [r.to_json() for r in removed]}

    @app.get("/platform/darwins/{darwin_id}/resource_throttle")
    async def get_resource_throttle(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            rows = _sup(request).list_resource_throttles(darwin_id)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"throttles": [r.to_json() for r in rows]}

    # -- fiat ramp (doc 17) -------------------------------------------------
    @app.post("/platform/fiat/deposit_quote")
    async def fiat_deposit_quote(
        request: Request,
        req: FiatDepositQuoteRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        try:
            quote = _sup(request).fiat_deposit_quote(
                amount_usd=req.amount_usd,
                payment_method=req.payment_method,
                actor=_actor(ctx),
            )
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return quote.to_json()

    @app.post("/platform/fiat/confirm_deposit")
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

    @app.post("/platform/fiat/withdraw")
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

    # -- platform signing key rotation (doc 02) ----------------------------
    @app.post("/platform/signing/rotate")
    async def rotate_signing_key(
        request: Request,
        req: RotateSigningKeyRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            summary = _sup(request).rotate_signing_key(
                drain_window_seconds=req.drain_window_seconds,
                actor=_actor(ctx),
            )
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return summary

    # -- Path-C: Darwin-generated neuron proposals (doc 04 phase 3) -------
    @app.get("/darwins/{darwin_id}/proposals")
    async def list_neuron_proposals(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            return _sup(request).list_neuron_proposals(darwin_id)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    @app.post("/platform/darwins/{darwin_id}/proposals/{name}/admit")
    async def admit_neuron_proposal(
        darwin_id: str,
        name: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            return _sup(request).admit_neuron_proposal(
                darwin_id, name, actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))

    @app.post("/platform/darwins/{darwin_id}/proposals/{name}/reject")
    async def reject_neuron_proposal(
        darwin_id: str,
        name: str,
        request: Request,
        req: RejectProposalRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            return _sup(request).reject_neuron_proposal(
                darwin_id, name, reason=req.reason, actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    # -- harvest rules (doc 05: scheduled profit withdrawal) ---------------
    @app.post("/darwins/{darwin_id}/harvest_rules", status_code=201)
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

    @app.get("/darwins/{darwin_id}/harvest_rules")
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

    @app.delete("/darwins/{darwin_id}/harvest_rules/{rule_id}")
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
                darwin_id, rule_id, actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(404, str(exc))
        return rule.to_json()

    # -- browser sessions (doc 15: cache + cost attribution) ---------------
    @app.get("/darwins/{darwin_id}/browser/sessions")
    async def browser_sessions(
        darwin_id: str,
        request: Request,
        limit: int | None = Query(None, ge=1, le=1024),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            rows = _sup(request).browser_sessions(darwin_id, limit=limit)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"darwin_id": darwin_id, "sessions": rows}

    @app.get("/platform/browser/stats")
    async def browser_stats(
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        return _sup(request).browser_stats()

    # -- vault admin (doc 16: bidirectional shared vault + KYC retention) --
    @app.post("/darwins/{darwin_id}/vault")
    async def vault_put(
        darwin_id: str,
        request: Request,
        req: VaultPutRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            ref = _sup(request).vault_put(
                darwin_id, tier=req.tier, key=req.key, value=req.value,
                scope=req.scope, ttl_seconds=req.ttl_seconds,
                original_filename=req.original_filename,
                actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        # Value is never echoed back.
        return ref.to_json()

    @app.delete("/darwins/{darwin_id}/vault")
    async def vault_delete(
        darwin_id: str,
        request: Request,
        tier: str = Query(..., pattern=r"^(public|scoped|sensitive)$"),
        key: str = Query(..., min_length=1),
        scope: str | None = Query(None),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            ref = _sup(request).vault_delete(
                darwin_id, tier=tier, key=key, scope=scope, actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(404, str(exc))
        return ref.to_json()

    @app.get("/darwins/{darwin_id}/vault")
    async def vault_list(
        darwin_id: str,
        request: Request,
        tier: str | None = Query(None, pattern=r"^(public|scoped|sensitive)$"),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            rows = _sup(request).list_vault_entries(darwin_id, tier=tier)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        # Values never returned.
        return {
            "darwin_id": darwin_id,
            "entries": [r.to_json() for r in rows],
        }

    @app.post("/platform/vault/sweep")
    async def vault_sweep(
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        purged = _sup(request).sweep_vault_retention()
        return {"purged": [r.to_json() for r in purged], "count": len(purged)}

    # -- provisioning (doc 04, 07: infrastructure.provisioner backend) -----
    @app.get("/darwins/{darwin_id}/resources")
    async def list_darwin_resources(
        darwin_id: str,
        request: Request,
        include_deprovisioned: bool = Query(False),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            rows = _sup(request).list_resources(
                darwin_id, include_deprovisioned=include_deprovisioned,
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {
            "darwin_id": darwin_id,
            "resources": [r.to_json() for r in rows],
        }

    @app.delete("/darwins/{darwin_id}/resources/{resource_id}")
    async def deprovision_resource(
        darwin_id: str,
        resource_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        try:
            record = await _sup(request).deprovision_resource(
                darwin_id, resource_id, actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except ProvisioningError as exc:
            raise HTTPException(409, str(exc))
        return record.to_json()

    @app.get("/platform/resources")
    async def fleet_resources(
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        return {"by_kind": _sup(request).provisioning_summary()}

    # -- alerts (doc 08: quotas + anomaly detection) ----------------------
    def _parse_severity(value: str | None) -> Severity | None:
        if value is None or value == "":
            return None
        try:
            return Severity(value)
        except ValueError:
            raise HTTPException(400, f"unknown severity: {value!r}")

    @app.get("/darwins/{darwin_id}/alerts")
    async def darwin_alerts(
        darwin_id: str,
        request: Request,
        min_severity: str | None = Query(None),
        since: float | None = Query(None),
        limit: int | None = Query(None, ge=1, le=10_000),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, OWNER)
        _require_owns(ctx, darwin_id)
        sev = _parse_severity(min_severity)
        try:
            rows = await _sup(request).alerts(
                darwin_id, min_severity=sev, since=since, limit=limit
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"darwin_id": darwin_id, "alerts": rows}

    @app.get("/platform/alerts")
    async def fleet_alerts(
        request: Request,
        min_severity: str | None = Query(None),
        since: float | None = Query(None),
        limit: int | None = Query(None, ge=1, le=10_000),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        sev = _parse_severity(min_severity)
        rows = await _sup(request).fleet_alerts(
            min_severity=sev, since=since, limit=limit
        )
        return {"alerts": rows}

    # -- internal: quarantine (doc 08 + doc 09 level 4) -------------------
    @app.post("/platform/darwins/{darwin_id}/quarantine")
    async def quarantine_darwin(
        darwin_id: str,
        request: Request,
        req: QuarantineRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            await _sup(request).quarantine(
                darwin_id, reason=req.reason, operator=req.operator,
                actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "quarantined"}

    @app.delete("/platform/darwins/{darwin_id}/quarantine")
    async def unquarantine_darwin(
        darwin_id: str,
        request: Request,
        operator: str = Query("platform"),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            await _sup(request).unquarantine(
                darwin_id, operator=operator, actor=_actor(ctx)
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"status": "cleared"}

    # -- internal: wallet policy (doc 08 POST /platform/wallets/.../policy)
    @app.post("/platform/wallets/{darwin_id}/policy")
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

    @app.get("/platform/wallets/{darwin_id}/policy")
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

    # -- internal: throttle (doc 09 level 2) ------------------------------
    @app.post("/platform/darwins/{darwin_id}/throttle")
    async def install_throttle(
        darwin_id: str,
        request: Request,
        req: ThrottleRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            return await _sup(request).throttle_llm_tier(
                darwin_id, tier=req.tier, max_daily_calls=req.max_daily_calls,
                actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(400, str(exc))

    @app.delete("/platform/darwins/{darwin_id}/throttle")
    async def clear_throttle(
        darwin_id: str,
        request: Request,
        tier: str | None = Query(None),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            return await _sup(request).clear_llm_throttle(
                darwin_id, tier=tier, actor=_actor(ctx)
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(400, str(exc))

    @app.get("/platform/darwins/{darwin_id}/throttle")
    async def get_throttles(
        darwin_id: str,
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            return {"throttles": await _sup(request).get_llm_throttles(darwin_id)}
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    # -- internal: forced reproduction with amendment (doc 02 + doc 09) ---
    @app.post("/platform/darwins/{darwin_id}/reproduce_with_amendment")
    async def reproduce_with_amendment(
        darwin_id: str,
        request: Request,
        req: AmendmentRequest,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        try:
            blob = base64.b64decode(req.amendment_blob_b64, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise HTTPException(400, f"amendment_blob_b64 must be base64: {exc}")
        try:
            await _sup(request).force_reproduce_with_amendment(
                darwin_id,
                amendment_blob=blob,
                amendment_name=req.amendment_name,
                kill_parent_reason=req.kill_parent_reason,
                actor=_actor(ctx),
            )
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "amendment-reproduction-requested"}

    # -- internal: signing public key (doc 08; HSM-backed in production) --
    @app.get("/platform/signing/public_key")
    async def signing_public_key(
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        pem = _sup(request).signing_public_key_pem().decode("ascii")
        return {"format": "PEM", "public_key": pem}

    # -- operator audit log (doc 08) --------------------------------------
    @app.get("/platform/audit")
    async def fleet_audit(
        request: Request,
        actor: str | None = Query(None),
        action: str | None = Query(None),
        darwin_id: str | None = Query(None),
        since: float | None = Query(None),
        limit: int | None = Query(None, ge=1, le=10_000),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        rows = await _sup(request).audit_entries(
            actor=actor, action=action, darwin_id=darwin_id,
            since=since, limit=limit,
        )
        return {"entries": rows, "tip_hash": _sup(request).audit_log.tip_hash()}

    # -- Phase 1 patch-staging status (doc 21) ----------------------------
    @app.get("/platform/staging/status")
    async def staging_status(
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        """Snapshot of the staging service: quota usage + recent verdicts.
        Used by the dashboard to show ops what mutation-evaluation
        pressure looks like across the fleet."""
        _require_scope(ctx, PLATFORM_OPERATOR)
        return _sup(request).patch_staging.status()

    @app.get("/platform/staging/history")
    async def staging_history(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, Any]:
        rows = _sup(request).patch_staging.history(limit=limit)
        return {"verdicts": [v.to_json() for v in rows], "total": len(rows)}

    # -- snapshot (ops-side; not in doc 08) -------------------------------
    @app.get("/platform/snapshot")
    async def snapshot_endpoint(
        request: Request, ctx: AuthContext = Depends(get_auth)
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        return build_snapshot(_sup(request))

    # -- liveness probe (unauthenticated; k8s readiness/liveness) --------
    # Doc 08 §"Quotas and anomaly detection" mentions heartbeat health
    # at the supervisor level; this is the supervisor process's own
    # heartbeat surface (am I alive at all?). Returns 200 with a small
    # status body so a probe + a human curl both work.
    @app.get("/healthz")
    async def healthz(request: Request) -> dict[str, Any]:
        sup = _sup(request)
        return {
            "status": "ok",
            "persistence": (
                sup._persistence.backend_kind  # noqa: SLF001
                if sup._persistence is not None  # noqa: SLF001
                else "memory"
            ),
            "uptime_seconds": time.time() - app_started_ts,
            "darwins": len(sup.list_ids()),
        }

    # -- readiness probe (HA-aware) --------------------------------------
    # ``/healthz`` is liveness — alive at all. ``/readyz`` is readiness —
    # the leader pod is ready, followers are NOT. k8s ``readinessProbe``
    # targets this so the Service routes API traffic only to the leader.
    # On single-replica deployments (NoLeaderElection), every probe
    # returns 200 since ``is_leader`` is always True.
    @app.get("/readyz")
    async def readyz(request: Request) -> Response:
        sup = _sup(request)
        # Start the election on first call so single-replica callers
        # don't need to know the lifecycle.
        await sup.ensure_leader_started()
        # Persistence health (doc 08 HA): a leader pod with a corrupt or
        # disconnected DB must NOT pass readiness, otherwise k8s would
        # route mutating traffic to a pod that crashes every spawn. A
        # cheap SELECT 1 catches connection drops and obvious corruption.
        persistence_ok = True
        persistence_error = None
        if sup._persistence is not None:  # noqa: SLF001
            try:
                sup._persistence._backend.execute("SELECT 1").fetchone()  # noqa: SLF001
            except Exception as exc:
                persistence_ok = False
                persistence_error = str(exc)[:200]
        import json as _json
        if not persistence_ok:
            return Response(
                content=_json.dumps({
                    "status": "persistence_down",
                    "leader": sup.is_leader,
                    "error": persistence_error,
                }),
                media_type="application/json",
                status_code=503,
            )
        if sup.is_leader:
            return Response(
                content=_json.dumps({"status": "ok", "leader": True}),
                media_type="application/json",
            )
        return Response(
            content=_json.dumps({"status": "follower", "leader": False}),
            media_type="application/json",
            status_code=503,
        )

    # -- Prometheus metrics (unauthenticated; same posture as /healthz) --
    # Doc 08 §Observability: the platform emits OpenTelemetry traces (we
    # do, via the tracing module) AND should expose scrapable metrics for
    # ops dashboards (Grafana / Honeycomb). Text format per
    # https://prometheus.io/docs/instrumenting/exposition_formats/ —
    # zero external deps required to render it.
    @app.get("/metrics")
    async def metrics(request: Request) -> Response:
        sup = _sup(request)
        return Response(
            content=_render_metrics(sup, app_started_ts),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    return app


# -- Prometheus exposition --------------------------------------------------
def _render_metrics(sup: Supervisor, started_ts: float) -> str:
    """Render the supervisor's current state as Prometheus text. No external
    dep — the format is simple text we can build directly. Pulled into a
    module-level function so tests can exercise it without spinning a full
    HTTP server."""
    lines: list[str] = []

    def metric(
        name: str,
        kind: str,
        help_: str,
        samples: list[tuple[dict[str, str], float]],
    ) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if labels:
                lbl = ",".join(
                    f'{k}="{_escape_label(v)}"' for k, v in sorted(labels.items())
                )
                lines.append(f"{name}{{{lbl}}} {_fmt(value)}")
            else:
                lines.append(f"{name} {_fmt(value)}")

    # -- per-Darwin state buckets ----------------------------------------
    alive = killed = paused = quarantined = 0
    for h in sup._darwins.values():  # noqa: SLF001
        if h.killed:
            killed += 1
        elif h.runtime.state.alive:
            alive += 1
        if h.runtime.state.paused:
            paused += 1
        if h.quarantine.active:
            quarantined += 1
    metric(
        "darwin_platform_darwins",
        "gauge",
        "Number of Darwins by state.",
        [
            ({"state": "alive"}, alive),
            ({"state": "killed"}, killed),
            ({"state": "paused"}, paused),
            ({"state": "quarantined"}, quarantined),
        ],
    )

    # -- audit log -------------------------------------------------------
    audit_total = len(sup.audit_log)
    metric(
        "darwin_platform_audit_entries_total",
        "counter",
        "Total operator audit entries ever written.",
        [({}, float(audit_total))],
    )

    # -- escalations -----------------------------------------------------
    open_escalations = sum(
        len(sup.router.for_darwin(did, include_resolved=False))
        for did in sup.list_ids()
    )
    metric(
        "darwin_platform_escalations_open",
        "gauge",
        "Currently-open escalation tickets across the fleet.",
        [({}, float(open_escalations))],
    )

    # -- alerts ----------------------------------------------------------
    severity_counts: dict[str, int] = {"info": 0, "warn": 0, "critical": 0}
    for alert in sup.monitor.all_alerts():
        severity_counts[alert.severity.value] = (
            severity_counts.get(alert.severity.value, 0) + 1
        )
    metric(
        "darwin_platform_alerts_total",
        "counter",
        "Monitor alerts emitted by severity, since process start (or "
        "restored from persistence).",
        [
            ({"severity": sev}, float(count))
            for sev, count in severity_counts.items()
        ],
    )

    # -- billing ---------------------------------------------------------
    fleet = sup.billing_service.fleet_summary()
    metric(
        "darwin_platform_billing_revenue_usdc",
        "gauge",
        "Total platform revenue (markup + harvest fees) across the fleet.",
        [({}, float(fleet["total"]["platform_revenue_usdc"]))],
    )
    metric(
        "darwin_platform_billing_harvest_fees_usdc",
        "gauge",
        "Total harvest fees collected across the fleet.",
        [({}, float(fleet["total"]["harvest_fees_usdc"]))],
    )

    # -- lineage --------------------------------------------------------
    edges = sup._lineage_registry.edges()  # noqa: SLF001
    metric(
        "darwin_platform_lineage_edges_total",
        "counter",
        "Parent->child lineage edges observed (across all reproductions).",
        [({}, float(len(edges)))],
    )

    # -- vault: watermarks issued + retention sweeps ---------------------
    vault_entries = sum(
        1 for _ in sup.vault_admin._index.values() for _ in _  # noqa: SLF001
    )
    metric(
        "darwin_platform_vault_entries_total",
        "gauge",
        "Vault entries currently tracked across the fleet.",
        [({}, float(vault_entries))],
    )
    watermarked = 0
    for darwin_id, by_path in sup.vault_admin._index.items():  # noqa: SLF001
        for ref in by_path.values():
            if ref.watermarked:
                watermarked += 1
    metric(
        "darwin_platform_vault_watermarked_total",
        "gauge",
        "Vault entries currently carrying an embedded HMAC watermark.",
        [({}, float(watermarked))],
    )

    # -- sanctions refusals (recorded in operator audit) -----------------
    sanctions_refusals = 0
    for entry in sup.audit_log.entries(action="wallet_error"):
        if "sanctions" in (entry.metadata.get("reason") or ""):
            sanctions_refusals += 1
    metric(
        "darwin_platform_sanctions_refusals_total",
        "counter",
        "Wallet outbound transfers refused by the sanctions screener.",
        [({}, float(sanctions_refusals))],
    )

    # -- resource throttles ----------------------------------------------
    throttles = sup._throttle_service  # noqa: SLF001
    for kind in ("sandbox", "browser", "egress"):
        used_total = 0
        installed_total = 0
        for h in sup._darwins.values():  # noqa: SLF001
            t = throttles.get(darwin_id=h.darwin_id, kind=kind)
            if t is not None:
                installed_total += 1
                used_total += throttles.used_today(
                    darwin_id=h.darwin_id, kind=kind,
                )
        metric(
            "darwin_platform_throttle_installed",
            "gauge",
            "Number of Darwins with an installed throttle for this resource.",
            [({"kind": kind}, float(installed_total))],
        )
        metric(
            "darwin_platform_throttle_used_today",
            "gauge",
            "Aggregate count of throttled calls consumed today by resource.",
            [({"kind": kind}, float(used_total))],
        )

    # -- browser cache ----------------------------------------------------
    bc_stats = sup.browser_stats()
    metric(
        "darwin_platform_browser_sessions_total",
        "counter",
        "Browser sessions recorded across the fleet.",
        [({}, float(bc_stats.get("total_sessions", 0)))],
    )
    metric(
        "darwin_platform_browser_cost_usdc",
        "gauge",
        "Total browser-session cost (USDC) attributed across the fleet.",
        [({}, float(bc_stats.get("total_cost_usdc", 0.0)))],
    )
    promoted = sum(
        1 for s in bc_stats.get("signatures", [])
        if s.get("promoted")
    )
    metric(
        "darwin_platform_browser_cache_promoted",
        "gauge",
        "Browser signatures currently promoted to the cached replay path.",
        [({}, float(promoted))],
    )

    # -- LLM circuit breaker state (only when LiteLLM transport is wired) -
    breaker_states: dict[str, int] = {"closed": 0, "open": 0, "half_open": 0}
    for h in sup._darwins.values():  # noqa: SLF001
        transport = h.runtime.llm._t  # noqa: SLF001
        cb = getattr(transport, "circuit_breaker", None)
        if cb is None:
            continue
        for _model, st in cb.snapshot().items():
            state = st.get("state", "closed")
            breaker_states[state] = breaker_states.get(state, 0) + 1
    metric(
        "darwin_platform_llm_circuit_breaker_models",
        "gauge",
        "Per-model LLM circuit-breaker state counts across the fleet.",
        [
            ({"state": s}, float(c))
            for s, c in breaker_states.items()
        ],
    )

    # -- resume events ----------------------------------------------------
    resume_entries = len(sup.audit_log.entries(action="resume"))
    metric(
        "darwin_platform_resume_events_total",
        "counter",
        "Number of Darwin resumes since process start (or restored from audit log).",
        [({}, float(resume_entries))],
    )

    # -- harvest rule fires + scheduler rules ----------------------------
    harvest_rule_fires = len(
        sup.audit_log.entries(action="harvest_rule_fired")
    )
    metric(
        "darwin_platform_harvest_rule_fires_total",
        "counter",
        "Recurring-harvest rule fires recorded by the scheduler.",
        [({}, float(harvest_rule_fires))],
    )
    installed_rules = len(sup._harvest_scheduler.list())  # noqa: SLF001
    metric(
        "darwin_platform_harvest_rules_installed",
        "gauge",
        "Currently-installed recurring harvest rules.",
        [({}, float(installed_rules))],
    )

    # -- Phase 1 cost-governance metrics (doc 21 §"Metering ...") --------
    # Per-Darwin daily spend (sum of debits today, in USDC). The
    # wallet's spent_today is authoritative; the platform reads through
    # it for the dashboard.
    daily_spend_samples: list[tuple[dict[str, str], float]] = []
    llm_cost_samples: list[tuple[dict[str, str], float]] = []
    neuron_cost_total: float = 0.0
    revenue_samples: list[tuple[dict[str, str], float]] = []
    for h in sup._darwins.values():  # noqa: SLF001
        try:
            spent = float(h.stack.wallet.spent_today_usdc())
        except Exception:
            spent = 0.0
        daily_spend_samples.append(
            ({"darwin_id": h.darwin_id}, float(spent))
        )
        # LLM cost: pull from the per-Darwin LLM router's running total
        # when the transport exposes one; otherwise zero. The router's
        # ``cost_today_usdc`` is the canonical genome-side counter.
        llm_cost = 0.0
        try:
            llm_cost = float(
                getattr(h.runtime.llm, "cost_today_usdc", lambda: 0.0)()
            )
        except Exception:
            llm_cost = 0.0
        llm_cost_samples.append(
            ({"darwin_id": h.darwin_id}, llm_cost)
        )
        # Revenue: read the genome's last analytics ROI output from
        # ``state.metrics`` when present. Best-effort; absence ⇒ 0.
        revenue = 0.0
        try:
            metrics_blob = getattr(h.runtime.state, "metrics", {}) or {}
            revenue = float(metrics_blob.get("revenue_usdc", 0.0))
        except Exception:
            revenue = 0.0
        revenue_samples.append(
            ({"darwin_id": h.darwin_id}, revenue)
        )
    metric(
        "darwin_daily_spend_usdc",
        "gauge",
        "Per-Darwin spend recorded so far today (USDC). Alert if > $20/day.",
        daily_spend_samples or [({}, 0.0)],
    )
    metric(
        "darwin_llm_cost_usdc",
        "gauge",
        "Per-Darwin LLM token spend today (USDC, from the genome's router).",
        llm_cost_samples or [({}, 0.0)],
    )
    # Neuron cost: aggregate count of distinct dispatches recorded on the
    # audit chain (each ``intent:neuron.*`` is one dispatch). Phase 1 uses
    # the chain rather than a per-neuron gauge so a restarted Darwin's
    # historical dispatches are preserved.
    for entry in sup.audit_log.entries(action="spawn"):
        # Spawn entries are not dispatches; we filter by counting via the
        # per-Darwin audit sinks below.
        continue
    for h in sup._darwins.values():  # noqa: SLF001
        try:
            rows = h.stack.audit_sink.read_all()
        except Exception:
            rows = []
        for r in rows:
            k = r.get("kind", "")
            if isinstance(k, str) and k.startswith("intent:neuron."):
                neuron_cost_total += 1.0
    metric(
        "darwin_neuron_dispatch_total",
        "counter",
        "Total neuron dispatches recorded on the genome audit chains.",
        [({}, neuron_cost_total)],
    )
    metric(
        "darwin_revenue_usdc",
        "gauge",
        "Per-Darwin observed revenue (USDC) from the latest analytics "
        "measurement.",
        revenue_samples or [({}, 0.0)],
    )

    # -- Phase 1 staging service metrics (doc 21) ------------------------
    try:
        staging_snap = sup.patch_staging.quota.snapshot()
        metric(
            "darwin_platform_staging_quota",
            "gauge",
            "Patch-staging concurrency: active vs. cap.",
            [
                ({"state": "active"}, float(staging_snap["active"])),
                ({"state": "max"}, float(staging_snap["max_concurrent"])),
            ],
        )
        metric(
            "darwin_platform_staging_evaluations_total",
            "counter",
            "Patch-staging evaluations acquired (total).",
            [({}, float(staging_snap["total_acquired"]))],
        )
        metric(
            "darwin_platform_staging_quota_rejections_total",
            "counter",
            "Patch-staging acquires refused due to quota.",
            [({}, float(staging_snap["total_rejected"]))],
        )
        staging_promotions = sum(
            1 for v in sup.patch_staging.history() if v.promote
        )
        metric(
            "darwin_platform_staging_promotions_total",
            "counter",
            "Patch-staging verdicts that promoted the candidate.",
            [({}, float(staging_promotions))],
        )
    except Exception:
        pass

    # -- process uptime --------------------------------------------------
    metric(
        "darwin_platform_uptime_seconds",
        "gauge",
        "Seconds since this supervisor process started.",
        [({}, time.time() - started_ts)],
    )

    return "\n".join(lines) + "\n"


def _escape_label(v: Any) -> str:
    """Escape a Prometheus label value per spec: backslash, double-quote,
    newline. Cheap; values here are short."""
    return (
        str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )


def _fmt(v: float) -> str:
    """Prometheus prefers integers as ints, floats with limited precision."""
    if isinstance(v, int) or float(v).is_integer():
        return str(int(v))
    return f"{v:.6f}"


# Default singleton for ``uvicorn darwin_platform.api:app``.
app = build_app()
