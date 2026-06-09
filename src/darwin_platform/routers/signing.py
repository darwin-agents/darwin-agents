"""Signing routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Signing' tag instead of the catch-all default.
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
    RotateSigningKeyRequest,
)

router = APIRouter(tags=["Signing"])


# -- platform signing key rotation (doc 02) ----------------------------
@router.post("/platform/signing/rotate")
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


# -- internal: signing public key (doc 08; HSM-backed in production) --
@router.get("/platform/signing/public_key")
async def signing_public_key(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, str]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    pem = _sup(request).signing_public_key_pem().decode("ascii")
    return {"format": "PEM", "public_key": pem}
