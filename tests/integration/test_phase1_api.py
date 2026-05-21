"""HTTP-layer tests for Phase 1 endpoints (docs/engineering/21).

Covers:
- ``GET /darwins/{id}/lineage/tree`` returns nested structure.
- ``GET /platform/staging/status`` + ``/staging/history`` return shape.
- Phase 1 metrics surface on ``/metrics``.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

from darwin_platform.api import build_app
from darwin_platform.supervisor import LineageEdge, Supervisor


@pytest.fixture
async def client():
    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            yield c, sup
        finally:
            await sup.shutdown()


async def test_lineage_tree_endpoint_returns_shape(client) -> None:
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]

    r = await c.get(f"/darwins/{did}/lineage/tree")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["darwin_id"] == did
    assert body["generation"] == 0
    assert body["ancestors"] == []
    assert body["descendants"] == []


async def test_lineage_tree_with_descendants(client) -> None:
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]

    # Inject a child + grandchild directly via the registry — exercises
    # the tree query without driving a real reproduction.
    sup._lineage_registry.add(LineageEdge(  # noqa: SLF001
        parent_id=did, child_id="kid", spawned_at=1.0, budget_usdc=20.0,
    ))
    sup._lineage_registry.add(LineageEdge(  # noqa: SLF001
        parent_id="kid", child_id="grandkid",
        spawned_at=2.0, budget_usdc=10.0,
    ))

    r = await c.get(f"/darwins/{did}/lineage/tree?max_depth=4")
    assert r.status_code == 200
    body = r.json()
    assert len(body["descendants"]) == 1
    kid = body["descendants"][0]
    assert kid["darwin_id"] == "kid"
    assert kid["budget_usdc"] == 20.0
    assert len(kid["children"]) == 1
    assert kid["children"][0]["darwin_id"] == "grandkid"


async def test_lineage_tree_404_on_unknown(client) -> None:
    c, _ = client
    r = await c.get("/darwins/does-not-exist/lineage/tree")
    assert r.status_code == 404


async def test_staging_status_endpoint(client) -> None:
    c, _ = client
    r = await c.get("/platform/staging/status")
    assert r.status_code == 200
    body = r.json()
    assert "quota" in body
    assert body["quota"]["max_concurrent"] >= 1
    assert body["history_size"] == 0
    assert body["recent"] == []


async def test_staging_history_endpoint(client) -> None:
    c, _ = client
    r = await c.get("/platform/staging/history?limit=10")
    assert r.status_code == 200
    body = r.json()
    assert "verdicts" in body
    assert body["total"] == 0


async def test_phase1_metrics_on_metrics_endpoint(client) -> None:
    c, sup = client
    # Spawn so the per-Darwin metric samples produce labelled output.
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]

    r = await c.get("/metrics")
    assert r.status_code == 200
    body = r.text
    # All four cost-governance gauges + the staging quota gauge should
    # be present in the exposition output.
    for name in (
        "darwin_daily_spend_usdc",
        "darwin_llm_cost_usdc",
        "darwin_neuron_dispatch_total",
        "darwin_revenue_usdc",
        "darwin_platform_staging_quota",
        "darwin_platform_staging_evaluations_total",
        "darwin_platform_staging_promotions_total",
    ):
        assert name in body, f"missing metric {name} in /metrics output"
    # The darwin_id label must appear on the per-Darwin gauges.
    assert f'darwin_id="{did}"' in body
