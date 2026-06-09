"""Ops routes.

Extracted from ``darwin_platform.api`` (see that module's docstring). All
handlers attach to a domain ``APIRouter`` so the OpenAPI/Swagger UI groups
them under the 'Ops' tag instead of the catch-all default.
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import (
    APIRouter,
    Request,
    Response,
)


from .deps import (
    _sup,
)
from .metrics_text import _render_metrics

router = APIRouter(tags=["Ops"])


# -- liveness probe (unauthenticated; k8s readiness/liveness) --------
# Doc 08 §"Quotas and anomaly detection" mentions heartbeat health
# at the supervisor level; this is the supervisor process's own
# heartbeat surface (am I alive at all?). Returns 200 with a small
# status body so a probe + a human curl both work.
@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    sup = _sup(request)
    return {
        "status": "ok",
        "persistence": (
            sup._persistence.backend_kind  # noqa: SLF001
            if sup._persistence is not None  # noqa: SLF001
            else "memory"
        ),
        "uptime_seconds": time.time() - request.app.state.started_ts,
        "darwins": len(sup.list_ids()),
    }


# -- readiness probe (HA-aware) --------------------------------------
# ``/healthz`` is liveness — alive at all. ``/readyz`` is readiness —
# the leader pod is ready, followers are NOT. k8s ``readinessProbe``
# targets this so the Service routes API traffic only to the leader.
# On single-replica deployments (NoLeaderElection), every probe
# returns 200 since ``is_leader`` is always True.
@router.get("/readyz")
async def readyz(request: Request) -> Response:
    sup = _sup(request)
    # Start the election on first call so single-replica callers
    # don't need to know the lifecycle.
    await sup.ensure_leader_started()
    # Persistence health (doc 08 HA): a leader pod with a corrupt or
    # disconnected DB must NOT pass readiness, otherwise k8s would
    # route mutating traffic to a pod that crashes every spawn. A
    # cheap SELECT 1 catches connection drops and obvious corruption.
    persistence_ok = True
    persistence_error = None
    if sup._persistence is not None:  # noqa: SLF001
        try:
            sup._persistence._backend.execute("SELECT 1").fetchone()  # noqa: SLF001
        except Exception as exc:
            persistence_ok = False
            persistence_error = str(exc)[:200]
    import json as _json

    if not persistence_ok:
        return Response(
            content=_json.dumps(
                {
                    "status": "persistence_down",
                    "leader": sup.is_leader,
                    "error": persistence_error,
                }
            ),
            media_type="application/json",
            status_code=503,
        )
    if sup.is_leader:
        return Response(
            content=_json.dumps({"status": "ok", "leader": True}),
            media_type="application/json",
        )
    return Response(
        content=_json.dumps({"status": "follower", "leader": False}),
        media_type="application/json",
        status_code=503,
    )


# -- Prometheus metrics (unauthenticated; same posture as /healthz) --
# Doc 08 §Observability: the platform emits OpenTelemetry traces (we
# do, via the tracing module) AND should expose scrapable metrics for
# ops dashboards (Grafana / Honeycomb). Text format per
# https://prometheus.io/docs/instrumenting/exposition_formats/ —
# zero external deps required to render it.
@router.get("/metrics")
async def metrics(request: Request) -> Response:
    sup = _sup(request)
    return Response(
        content=_render_metrics(sup, request.app.state.started_ts),
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
