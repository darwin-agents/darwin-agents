"""Freelancers routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Freelancers' tag instead of the catch-all default.
"""

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
)

from ..runtime.supervisor import SupervisorError
from ..security.auth import (
    PLATFORM_OPERATOR,
    AuthContext,
)

from .deps import (
    _actor,
    _require_scope,
    _sup,
    get_auth,
)
from .models import (
    RegisterFreelancerRequest,
)

router = APIRouter(tags=["Freelancers"])


# -- freelancer registry CRUD (doc 17) ---------------------------------
@router.post("/platform/freelancers")
async def register_freelancer(
    request: Request,
    req: RegisterFreelancerRequest,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return _sup(request).register_freelancer(
            owner_subject=req.owner_subject,
            freelancer_id=req.freelancer_id,
            fiat_destination=req.fiat_destination,
            display_name=req.display_name,
            kyc_status=req.kyc_status,
            metadata=req.metadata,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.delete("/platform/freelancers/{owner_subject}/{freelancer_id}")
async def revoke_freelancer(
    owner_subject: str,
    freelancer_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return _sup(request).revoke_freelancer(
            owner_subject=owner_subject,
            freelancer_id=freelancer_id,
            actor=_actor(ctx),
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/platform/freelancers/{owner_subject}")
async def list_freelancers(
    owner_subject: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> list[dict[str, Any]]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return _sup(request).list_freelancers(owner_subject=owner_subject)
