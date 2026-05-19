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
    from fastapi import Body, FastAPI, HTTPException, Query, Request
    from pydantic import BaseModel, Field
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "darwin_platform.api requires the [platform] extra: "
        "`pip install darwin-dna[platform]`"
    ) from exc

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


# -- app factory ------------------------------------------------------------
def build_app(supervisor: Supervisor | None = None) -> FastAPI:
    """Build a FastAPI app bound to ``supervisor`` (or a fresh one).

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

    def _sup(request: Request) -> Supervisor:
        return request.app.state.supervisor

    # -- spawn / status ----------------------------------------------------
    @app.post("/darwins", status_code=201)
    async def spawn(req: SpawnRequest, request: Request) -> dict[str, Any]:
        try:
            status = await _sup(request).spawn(
                opening_usdc=req.opening_usdc,
                escalation_auto=req.escalation_auto,
                parent_id=req.parent_id,
            )
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return asdict(status)

    @app.get("/darwins")
    async def list_darwins(request: Request) -> dict[str, Any]:
        sup = _sup(request)
        return {"darwins": sup.list_ids()}

    @app.get("/darwins/{darwin_id}")
    async def status(darwin_id: str, request: Request) -> dict[str, Any]:
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
    ) -> dict[str, str]:
        try:
            await _sup(request).pause(darwin_id, reason=req.reason)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(409, str(exc))
        return {"status": "pausing"}

    @app.post("/darwins/{darwin_id}/resume")
    async def resume(darwin_id: str, request: Request) -> dict[str, str]:
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
    ) -> dict[str, str]:
        try:
            await _sup(request).kill(darwin_id, reason=req.reason)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        return {"status": "killing"}

    # -- force reproduction -----------------------------------------------
    @app.post("/darwins/{darwin_id}/reproduce/force")
    async def reproduce_force(darwin_id: str, request: Request) -> dict[str, str]:
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
    ) -> dict[str, str]:
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
    ) -> dict[str, Any]:
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
        darwin_id: str, escalation_id: str, request: Request
    ) -> dict[str, Any]:
        try:
            return await _sup(request).get_escalation(darwin_id, escalation_id)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))
        except SupervisorError as exc:
            raise HTTPException(404, str(exc))

    # -- lineage / ledger / events ----------------------------------------
    @app.get("/darwins/{darwin_id}/lineage")
    async def lineage(darwin_id: str, request: Request) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
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
    ) -> dict[str, Any]:
        try:
            return await _sup(request).events(darwin_id, limit=limit, offset=offset)
        except UnknownDarwin as exc:
            raise HTTPException(404, str(exc))

    # -- snapshot (ops-side; not in doc 08) -------------------------------
    @app.get("/platform/snapshot")
    async def snapshot_endpoint(request: Request) -> dict[str, Any]:
        return build_snapshot(_sup(request))

    return app


# Default singleton for ``uvicorn darwin_platform.api:app``.
app = build_app()
