"""Resources routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Resources' tag instead of the catch-all default.
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

from ..runtime.supervisor import UnknownDarwin
from ..security.auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
)
from ..tenancy.provisioning import ProvisioningError

from .deps import (
    _actor,
    _require_owns,
    _require_scope,
    _sup,
    get_auth,
)

router = APIRouter(tags=["Resources"])


# -- browser sessions (doc 15: cache + cost attribution) ---------------
@router.get("/darwins/{darwin_id}/browser/sessions")
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


@router.get("/platform/browser/stats")
async def browser_stats(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return _sup(request).browser_stats()


# -- provisioning (doc 04, 07: infrastructure.provisioner backend) -----
@router.get("/darwins/{darwin_id}/resources")
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
            darwin_id,
            include_deprovisioned=include_deprovisioned,
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {
        "darwin_id": darwin_id,
        "resources": [r.to_json() for r in rows],
    }


@router.delete("/darwins/{darwin_id}/resources/{resource_id}")
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
            darwin_id,
            resource_id,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except ProvisioningError as exc:
        raise HTTPException(409, str(exc))
    return record.to_json()


@router.get("/platform/resources")
async def fleet_resources(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return {"by_kind": _sup(request).provisioning_summary()}
