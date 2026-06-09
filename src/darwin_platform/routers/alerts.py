"""Alerts routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Alerts' tag instead of the catch-all default.
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

from ..observability.monitoring import Severity
from ..runtime.supervisor import UnknownDarwin
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

router = APIRouter(tags=["Alerts"])


# -- alerts (doc 08: quotas + anomaly detection) ----------------------
def _parse_severity(value: str | None) -> Severity | None:
    if value is None or value == "":
        return None
    try:
        return Severity(value)
    except ValueError:
        raise HTTPException(400, f"unknown severity: {value!r}")


@router.get("/darwins/{darwin_id}/alerts")
async def darwin_alerts(
    darwin_id: str,
    request: Request,
    min_severity: str | None = Query(None),
    since: float | None = Query(None),
    limit: int | None = Query(None, ge=1, le=10_000),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, OWNER)
    _require_owns(ctx, darwin_id)
    sev = _parse_severity(min_severity)
    try:
        rows = await _sup(request).alerts(darwin_id, min_severity=sev, since=since, limit=limit)
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    return {"darwin_id": darwin_id, "alerts": rows}


@router.get("/platform/alerts")
async def fleet_alerts(
    request: Request,
    min_severity: str | None = Query(None),
    since: float | None = Query(None),
    limit: int | None = Query(None, ge=1, le=10_000),
    ctx: AuthContext = Depends(get_auth),
) -> dict[str, Any]:
    _require_scope(ctx, PLATFORM_OPERATOR)
    sev = _parse_severity(min_severity)
    rows = await _sup(request).fleet_alerts(min_severity=sev, since=since, limit=limit)
    return {"alerts": rows}
