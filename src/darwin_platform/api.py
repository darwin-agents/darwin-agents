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

from contextlib import asynccontextmanager
from dataclasses import asdict
from typing import Any

try:  # pragma: no cover - import-time guard
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "darwin_platform.api requires the [platform] extra: "
        "`pip install darwin-dna[platform]`"
    ) from exc

from .auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
    TokenStore,
    disabled_context,
)
from .monitoring import Severity
from .snapshot import snapshot as build_snapshot
from .supervisor import Supervisor, SupervisorError, UnknownDarwin


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


class WalletPolicyRequest(BaseModel):
    per_action_cap_usdc: float | None = Field(default=None, ge=0)
    per_day_cap_usdc: float | None = Field(default=None, ge=0)
    per_lifetime_cap_usdc: float | None = Field(default=None, ge=0)


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

    @asynccontextmanager
    async def lifespan(app_: FastAPI):
        # The supervisor is bound below so we can also support build_app()
        # being called with an explicit instance — keep this thin.
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
            await _sup(request).pause(darwin_id, reason=req.reason)
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
            await _sup(request).resume(darwin_id)
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
            await _sup(request).kill(darwin_id, reason=req.reason)
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
            await _sup(request).force_reproduce(darwin_id)
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
                darwin_id, reason=req.reason, operator=req.operator
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
            await _sup(request).unquarantine(darwin_id, operator=operator)
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

    # -- internal: signing public key (doc 08; HSM-backed in production) --
    @app.get("/platform/signing/public_key")
    async def signing_public_key(
        request: Request,
        ctx: AuthContext = Depends(get_auth),
    ) -> dict[str, str]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        pem = _sup(request).signing_public_key_pem().decode("ascii")
        return {"format": "PEM", "public_key": pem}

    # -- snapshot (ops-side; not in doc 08) -------------------------------
    @app.get("/platform/snapshot")
    async def snapshot_endpoint(
        request: Request, ctx: AuthContext = Depends(get_auth)
    ) -> dict[str, Any]:
        _require_scope(ctx, PLATFORM_OPERATOR)
        return build_snapshot(_sup(request))

    return app


# Default singleton for ``uvicorn darwin_platform.api:app``.
app = build_app()
