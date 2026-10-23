"""Darwins routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Darwins' tag instead of the catch-all default.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
)

from ..runtime.supervisor import DarwinStatus, SupervisorError, UnknownDarwin
from ..security.auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
    TokenStore,
)

from .deps import (
    _actor,
    _require_owns,
    _require_scope,
    _sup,
    get_auth,
)
from .models import (
    DisabledCategoriesRequest,
    LineageTree,
    DisabledNeuronsRequest,
    KillRequest,
    PauseRequest,
    ReproductionBlockRequest,
    SpawnRequest,
    SpendLockRequest,
)

router = APIRouter(tags=["Darwins"])


# -- spawn / status ----------------------------------------------------
@router.post("/darwins", status_code=201)
async def spawn(
    req: SpawnRequest,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    # Either an owner spawning their own Darwin or a platform operator
    # provisioning on behalf of one — either way the OWNER scope check
    # passes (operator implies owner).
    _require_scope(ctx, OWNER)
    # Owner consent (doc 01 step 3 + doc 05). Enforced in production
    # (require_spawn_consent); captured + audit-recorded always.
    if request.app.state.require_spawn_consent and not (req.terms_accepted and req.consent_18_plus):
        raise HTTPException(
            400,
            "spawn requires accepting the kill-switch terms "
            "(terms_accepted) and an 18+ attestation (consent_18_plus)",
        )
    try:
        status = await _sup(request).spawn(
            opening_usdc=req.opening_usdc,
            escalation_auto=req.escalation_auto,
            parent_id=req.parent_id,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    # Persist the consent against the new Darwin for the audit trail.
    try:
        _sup(request).audit_log.record(
            actor=_actor(ctx),
            action="spawn_consent",
            darwin_id=status.darwin_id,
            metadata={
                "terms_accepted": bool(req.terms_accepted),
                "consent_18_plus": bool(req.consent_18_plus),
            },
        )
    except Exception:
        pass
    # Grant the caller ownership of the newly minted id so subsequent
    # owner-scope calls succeed without an out-of-band grant.
    store: TokenStore | None = request.app.state.token_store
    if store is not None and ctx.token:
        store.grant(ctx.token, status.darwin_id)
    return asdict(status)


@router.get("/darwins")
async def list_darwins(request: Request, ctx: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    sup = _sup(request)
    # Platform operators see the whole fleet; owners see only their own.
    if ctx.has_scope(PLATFORM_OPERATOR):
        return {"darwins": sup.list_ids()}
    own = [d for d in sup.list_ids() if ctx.owns(d)]
    return {"darwins": own}


@router.get("/darwins/{darwin_id}")
async def status(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> DarwinStatus:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).status(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- pause / resume / kill --------------------------------------------
@router.post("/darwins/{darwin_id}/pause")
async def pause(
    darwin_id: str,
    request: Request,
    req: PauseRequest = Body(default=PauseRequest()),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        await _sup(request).pause(darwin_id, reason=req.reason, actor=_actor(ctx))
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    return {"status": "pausing"}


@router.post("/darwins/{darwin_id}/resume")
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


@router.post("/darwins/{darwin_id}/kill")
async def kill(
    darwin_id: str,
    request: Request,
    req: KillRequest = Body(default=KillRequest()),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        await _sup(request).kill(darwin_id, reason=req.reason, actor=_actor(ctx))
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {"status": "killing"}


# -- force reproduction -----------------------------------------------
@router.post("/darwins/{darwin_id}/reproduce/force")
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


# -- owner runtime controls (doc 09) ----------------------------------
@router.post("/darwins/{darwin_id}/controls/spend_lock")
async def set_spend_lock(
    darwin_id: str,
    request: Request,
    req: SpendLockRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).set_spend_lock(
            darwin_id,
            locked=req.locked,
            reason=req.reason,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.post("/darwins/{darwin_id}/controls/reproduction_block")
async def set_reproduction_block(
    darwin_id: str,
    request: Request,
    req: ReproductionBlockRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).set_reproduction_block(
            darwin_id,
            blocked=req.blocked,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.post("/darwins/{darwin_id}/controls/disabled_neurons")
async def set_disabled_neurons(
    darwin_id: str,
    request: Request,
    req: DisabledNeuronsRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).set_disabled_neurons(
            darwin_id,
            neurons=req.neurons,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.post("/darwins/{darwin_id}/controls/disabled_categories")
async def set_disabled_categories(
    darwin_id: str,
    request: Request,
    req: DisabledCategoriesRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).set_disabled_categories(
            darwin_id,
            categories=req.categories,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/darwins/{darwin_id}/controls")
async def get_owner_controls(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).get_owner_controls(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- anatomy snapshot (doc 05 / frontend 05) --------------------------
@router.get("/darwins/{darwin_id}/anatomy")
async def anatomy(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Live organism map. Sourced from runtime catalog + recent audit
    log; cheap enough to refetch on every WS reconnect."""
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).anatomy(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


# -- lineage / ledger / events ----------------------------------------
@router.get("/darwins/{darwin_id}/lineage")
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


@router.get("/darwins/{darwin_id}/lineage/tree")
async def lineage_tree(
    darwin_id: str,
    request: Request,
    max_depth: int = Query(16, ge=1, le=128),
    ctx: AuthContext = Depends(get_auth),
) -> LineageTree:
    """Phase 1 (doc 21 §"Lineage tree"): nested ancestors + descendants
    for the minimal dashboard. ``max_depth`` bounds the descendant
    recursion."""
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).lineage_tree(darwin_id, max_depth=max_depth)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))


@router.get("/darwins/{darwin_id}/events")
async def events(
    darwin_id: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    since: int | None = Query(
        None,
        ge=0,
        description=(
            "Tail cursor: return only events with seq > since, "
            "oldest-first. Use for incremental polling and WS resync."
        ),
    ),
    cursor: int | None = Query(
        None,
        ge=0,
        description=(
            "Head cursor: return events with seq < cursor, newest-first. "
            "Omit to start from the head."
        ),
    ),
    kind: str | None = Query(
        None,
        description="Audit-log kind prefix filter (e.g. 'event:payment').",
    ),
    neuron_id: str | None = Query(
        None,
        description="Filter to events emitted by or about this neuron.",
    ),
    tier: str | None = Query(
        None,
        description="LLM tier filter (payload.tier match).",
    ),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).events(
            darwin_id,
            limit=limit,
            offset=offset,
            since=since,
            cursor=cursor,
            kind=kind,
            neuron_id=neuron_id,
            tier=tier,
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
