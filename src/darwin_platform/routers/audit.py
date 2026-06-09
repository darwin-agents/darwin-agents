"""Audit & Staging routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Audit & Staging' tag instead of the catch-all default.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Query,
    Request,
)

from ..observability.snapshot import snapshot as build_snapshot
from ..security.auth import (
    PLATFORM_OPERATOR,
    AuthContext,
)

from .deps import (
    _require_scope,
    _sup,
    get_auth,
)

router = APIRouter(tags=["Audit & Staging"])


# -- operator audit log (doc 08) --------------------------------------
@router.get("/platform/audit")
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
        actor=actor,
        action=action,
        darwin_id=darwin_id,
        since=since,
        limit=limit,
    )
    return {"entries": rows, "tip_hash": _sup(request).audit_log.tip_hash()}


# -- Phase 1 patch-staging status (doc 21) ----------------------------
@router.get("/platform/staging/status")
async def staging_status(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Snapshot of the staging service: quota usage + recent verdicts.
    Used by the dashboard to show ops what mutation-evaluation
    pressure looks like across the fleet."""
    _require_scope(ctx, PLATFORM_OPERATOR)
    return _sup(request).patch_staging.status()


@router.get("/platform/staging/history")
async def staging_history(
    request: Request,
    limit: int = Query(50, ge=1, le=500),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    rows = _sup(request).patch_staging.history(limit=limit)
    return {"verdicts": [v.to_json() for v in rows], "total": len(rows)}


# -- snapshot (ops-side; not in doc 08) -------------------------------
@router.get("/platform/snapshot")
async def snapshot_endpoint(
    request: Request, ctx: AuthContext = Depends(get_auth)
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return build_snapshot(_sup(request))
