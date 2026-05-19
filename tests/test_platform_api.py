"""Smoke test for the Supervisor's FastAPI surface (docs/engineering/08).

End-to-end through the ASGI transport: every endpoint the doc-08 supervisor
API exposes is exercised at least once on a real Darwin spawned in the same
process. ``fastapi`` and ``httpx`` are in the ``[platform]`` extra; if either
is missing, the whole module is skipped — the genome's tests must still run
on a slim install.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

from darwin_platform.api import build_app  # noqa: E402
from darwin_platform.supervisor import Supervisor  # noqa: E402


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


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


async def test_spawn_status_kill_flow(client) -> None:
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    assert r.status_code == 201, r.text
    spawned = r.json()
    did = spawned["darwin_id"]
    assert spawned["alive"] is True
    assert spawned["balance_usdc"] == pytest.approx(60.0)

    r = await c.get(f"/darwins/{did}")
    assert r.status_code == 200
    assert r.json()["darwin_id"] == did

    r = await c.get("/darwins")
    assert did in r.json()["darwins"]

    # Drive a few ticks then kill.
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    r = await c.post(f"/darwins/{did}/kill", json={"reason": "smoke"})
    assert r.status_code == 200
    await _wait_until(lambda: not h.runtime.state.alive)
    r = await c.get(f"/darwins/{did}")
    body = r.json()
    assert body["alive"] is False
    assert body["killed"] is True
    assert "killed" in (body["death_reason"] or "")


async def test_pause_resume_endpoints(client) -> None:
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 3)

    r = await c.post(f"/darwins/{did}/pause", json={"reason": "ui"})
    assert r.status_code == 200
    await _wait_until(lambda: h.runtime.state.paused)

    r = await c.post(f"/darwins/{did}/resume")
    assert r.status_code == 200
    await _wait_until(lambda: not h.runtime.state.paused)


async def test_escalation_response_endpoint(client) -> None:
    c, sup = client
    r = await c.post(
        "/darwins", json={"opening_usdc": 60.0, "escalation_auto": None}
    )
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    h.stack.escalation.post(
        {"ticket": "T-9", "neuron": "browser.operator", "ticket_class": "approval"}
    )
    await _wait_until(lambda: sup.router.get("T-9") is not None)

    r = await c.get(f"/darwins/{did}/escalations")
    body = r.json()
    rows = body["tickets"]
    t9 = next(t for t in rows if t["ticket_id"] == "T-9")
    assert t9["ticket_class"] == "approval"
    # Approval class is 24h per the constitution.
    assert t9["remaining_seconds"] == pytest.approx(24 * 3600.0, rel=0.01)

    # Per-ticket detail endpoint.
    r = await c.get(f"/darwins/{did}/escalations/T-9")
    assert r.status_code == 200
    assert r.json()["ticket_id"] == "T-9"

    r = await c.post(
        f"/darwins/{did}/escalations/T-9/respond",
        json={"decision": "approve", "data": {"note": "ok"}},
    )
    assert r.status_code == 200

    r = await c.get(f"/darwins/{did}/escalations")
    assert all(t["ticket_id"] != "T-9" for t in r.json()["tickets"])

    # include_resolved brings it back.
    r = await c.get(f"/darwins/{did}/escalations", params={"include_resolved": True})
    rows = r.json()["tickets"]
    t9 = next(t for t in rows if t["ticket_id"] == "T-9")
    assert t9["status"] == "responded"
    assert t9["response"]["decision"] == "approve"


async def test_invalid_escalation_decision_400(client) -> None:
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]
    r = await c.post(
        f"/darwins/{did}/escalations/T-1/respond",
        json={"decision": "maybe"},
    )
    # Pydantic's regex pattern on the request model rejects the unknown
    # decision before it reaches the supervisor.
    assert r.status_code == 422


async def test_lineage_ledger_events_endpoints(client) -> None:
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    r = await c.get(f"/darwins/{did}/lineage")
    body = r.json()
    assert body["darwin_id"] == did
    assert body["children"] == []
    assert body["parent_id"] is None

    r = await c.get(f"/darwins/{did}/ledger", params={"limit": 5})
    body = r.json()
    assert body["limit"] == 5
    assert "movements" in body
    assert body["balance_usdc"] <= 60.0

    r = await c.get(f"/darwins/{did}/events", params={"limit": 5})
    body = r.json()
    assert body["limit"] == 5
    assert body["total"] >= 1  # at least the birth event


async def test_404_on_unknown_darwin(client) -> None:
    c, _ = client
    r = await c.get("/darwins/does-not-exist")
    assert r.status_code == 404
    r = await c.post("/darwins/does-not-exist/kill", json={"reason": "x"})
    assert r.status_code == 404


async def test_snapshot_endpoint(client) -> None:
    c, _ = client
    await c.post("/darwins", json={"opening_usdc": 60.0})
    r = await c.get("/platform/snapshot")
    body = r.json()
    assert body["schema"] == "darwin-platform.snapshot/1"
    assert len(body["darwins"]) == 1
