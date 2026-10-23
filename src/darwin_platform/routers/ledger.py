"""Ledger & Billing routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Ledger & Billing' tag instead of the catch-all default.
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
from .models import BillingSummary
from ..security.auth import (
    OWNER,
    PLATFORM_OPERATOR,
    AuthContext,
)

from .deps import (
    _require_owns,
    _require_scope,
    _sup,
    get_auth,
)

router = APIRouter(tags=["Ledger & Billing"])


@router.get("/darwins/{darwin_id}/ledger")
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
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/darwins/{darwin_id}/ledger.csv")
async def ledger_csv(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
):
    """Jurisdiction-neutral CSV export of the ledger (docs 05, 17) — the
    owner hands this to their accountant; the platform does not file
    taxes."""
    from fastapi.responses import Response

    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        csv_text = await _sup(request).ledger_csv(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))
    return Response(
        content=csv_text,
        media_type="text/csv",
        headers={
            "Content-Disposition": (f'attachment; filename="darwin-{darwin_id}-ledger.csv"'),
        },
    )


@router.get("/darwins/{darwin_id}/ledger/summary")
async def ledger_summary(
    darwin_id: str,
    request: Request,
    year: int | None = None,
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    """Jurisdiction-neutral accounting summary (docs 05, 17): totals
    (in/out/net) + per-category breakdown for a tax ``year`` (UTC), or
    lifetime when omitted. The owner hands this to their accountant;
    the platform does not file taxes or generate statutory forms."""
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).ledger_summary(darwin_id, year=year)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/darwins/{darwin_id}/billing")
async def darwin_billing(
    darwin_id: str,
    request: Request,
    ctx: AuthContext = Depends(get_auth),
) -> BillingSummary:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    try:
        return await _sup(request).billing(darwin_id)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))


@router.get("/platform/billing")
async def fleet_billing(request: Request, ctx: AuthContext = Depends(get_auth)) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    return await _sup(request).fleet_billing()
