"""Vault routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Vault' tag instead of the catch-all default.
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

from ..runtime.supervisor import SupervisorError, UnknownDarwin
from ..security.auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
)

from .deps import (
    _actor,
    _require_owns,
    _require_scope,
    _sup,
    get_auth,
)
from .models import (
    VaultPutRequest,
)

router = APIRouter(tags=["Vault"])


# -- vault admin (doc 16: bidirectional shared vault + KYC retention) --
@router.post("/darwins/{darwin_id}/vault")
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
            darwin_id,
            tier=req.tier,
            key=req.key,
            value=req.value,
            scope=req.scope,
            ttl_seconds=req.ttl_seconds,
            original_filename=req.original_filename,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    # Value is never echoed back.
    return ref.to_json()


@router.delete("/darwins/{darwin_id}/vault")
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
            darwin_id,
            tier=tier,
            key=key,
            scope=scope,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(404, str(exc))
    return ref.to_json()


@router.get("/darwins/{darwin_id}/vault")
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


@router.post("/platform/vault/sweep")
async def vault_sweep(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    purged = _sup(request).sweep_vault_retention()
    return {"purged": [r.to_json() for r in purged], "count": len(purged)}
