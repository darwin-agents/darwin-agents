"""Proposals routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Proposals' tag instead of the catch-all default.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
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
    RejectProposalRequest,
)

router = APIRouter(tags=["Proposals"])


# -- Path-C: Darwin-generated neuron proposals (doc 04 phase 3) -------
@router.get("/darwins/{darwin_id}/proposals")
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


@router.post("/platform/darwins/{darwin_id}/proposals/{name}/admit")
async def admit_neuron_proposal(
    darwin_id: str,
    name: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return _sup(request).admit_neuron_proposal(
            darwin_id,
            name,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.post("/platform/darwins/{darwin_id}/proposals/{name}/reject")
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
            darwin_id,
            name,
            reason=req.reason,
            actor=_actor(ctx),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
