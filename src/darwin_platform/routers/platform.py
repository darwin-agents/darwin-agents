"""Platform routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Platform' tag instead of the catch-all default.
"""

from __future__ import annotations

import base64
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
    AmendmentRequest,
)

router = APIRouter(tags=["Platform"])


# -- deployment capabilities (dashboard UI gating) --------------------
@router.get("/platform/capabilities")
async def platform_capabilities(
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, bool]:
    # What features this deployment exposes AND the caller may use here.
    # The dashboard hides tabs that would only ever 403/409: the organism
    # data browser (needs tenant namespaces + operator scope) and the
    # forensics viewer (needs a configured soma root + operator scope).
    # Any authenticated token may read it; the gated booleans fold in the
    # caller's operator scope so an owner token sees them False.
    caps = _sup(request).platform_capabilities()
    is_operator = ctx.has_scope(PLATFORM_OPERATOR)
    return {
        **caps,
        "organism_browser": caps["tenant_namespaces"] and is_operator,
        "forensics_viewer": caps["soma_forensics"] and is_operator,
    }


# -- post-mortem soma forensics (doc 05) -------------------------------
@router.get("/platform/darwins/{darwin_id}/forensics")
async def list_soma_forensics(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    # Operator post-mortem surface: the death-time soma snapshots for a
    # (usually dead) Darwin. Metadata only — contents are fetched per
    # snapshot. A Darwin with no captures returns an empty list, not 404,
    # so the index is queryable for any id.
    _require_scope(ctx, PLATFORM_OPERATOR)
    return {"snapshots": _sup(request).list_soma_forensics(darwin_id)}


@router.get("/platform/darwins/{darwin_id}/forensics/{snapshot_id}")
async def read_soma_forensic(
    darwin_id: str,
    snapshot_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    # The full manifest including the captured soma file contents.
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        return _sup(request).read_soma_forensic(darwin_id, snapshot_id)
    except SupervisorError as exc:
        raise HTTPException(404, str(exc))


# -- organism data browser (docs 08, 22) ------------------------------
@router.get("/platform/darwins/{darwin_id}/organism/tables")
async def list_organism_tables(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    # Operator data browser: the public tables in a Darwin's *business*
    # database, read through the SELECT-only dashboard role. A Darwin
    # without provisioned tenant namespaces has no organism DB -> 409.
    from ..adapters.organism_reader import OrganismReaderError

    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        tables = _sup(request).list_organism_tables(darwin_id)
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    except OrganismReaderError as exc:
        raise HTTPException(502, str(exc))
    return {"darwin_id": darwin_id, "tables": tables}


@router.get("/platform/darwins/{darwin_id}/organism/tables/{table}/rows")
async def read_organism_rows(
    darwin_id: str,
    table: str,
    request: Request,
    limit: int = Query(50, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    # A bounded, read-only page of one organism table. The table name is
    # validated as an identifier by the reader (it can't be a bound param);
    # an invalid name or read failure is a 400/502, not a 500.
    from ..adapters.organism_reader import OrganismReaderError

    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        page = _sup(request).read_organism_rows(
            darwin_id,
            table,
            limit=limit,
            offset=offset,
        )
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    except OrganismReaderError as exc:
        # Invalid identifier is a client error; a connection/read failure
        # is upstream. Both carry the reader's message, never the DSN.
        msg = str(exc)
        status = 400 if "invalid" in msg else 502
        raise HTTPException(status, msg)
    return {"darwin_id": darwin_id, **page}


# -- SMS 2FA inbox (doc 15) -------------------------------------------
@router.get("/platform/sms/verification-code")
async def sms_verification_code(
    request: Request,
    number: str | None = Query(None),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Latest inbound 2FA code from the platform's Twilio inbox — the
    operator (or an automation) reads it to answer an accounts.manager
    'need the SMS code' escalation. Platform-operator scope: the Twilio
    number is shared, so codes are not owner-readable."""
    _require_scope(ctx, PLATFORM_OPERATOR)
    try:
        result = _sup(request).sms_verification_code(number=number)
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    if result is None:
        raise HTTPException(404, "no verification code in the inbox")
    return result


# -- internal: forced reproduction with amendment (doc 02 + doc 09) ---
@router.post("/platform/darwins/{darwin_id}/reproduce_with_amendment")
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
