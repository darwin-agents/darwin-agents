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
from darwin_platform.runtime.supervisor import Supervisor  # noqa: E402


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


async def test_owner_control_endpoints(client) -> None:
    """The doc-09 owner runtime controls over the HTTP surface."""
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 3)

    r = await c.post(
        f"/darwins/{did}/controls/spend_lock",
        json={"locked": True, "reason": "owner away"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["spend_locked"] is True

    r = await c.post(
        f"/darwins/{did}/controls/reproduction_block", json={"blocked": True}
    )
    assert r.status_code == 200
    assert r.json()["reproduction_blocked"] is True

    r = await c.post(
        f"/darwins/{did}/controls/disabled_neurons",
        json={"neurons": ["ads.manager"]},
    )
    assert r.status_code == 200
    r = await c.post(
        f"/darwins/{did}/controls/disabled_categories",
        json={"categories": ["trading.crypto"]},
    )
    assert r.status_code == 200

    r = await c.get(f"/darwins/{did}/controls")
    assert r.status_code == 200
    body = r.json()
    assert body["spend_lock"]["spend_locked"] is True
    assert body["reproduction_blocked"] is True
    assert body["disabled_neurons"] == ["ads.manager"]
    assert body["disabled_categories"] == ["trading.crypto"]

    r = await c.post(
        "/darwins/does-not-exist/controls/spend_lock", json={"locked": True}
    )
    assert r.status_code == 404


async def test_ledger_csv_export(client) -> None:
    """The doc-05/17 jurisdiction-neutral ledger CSV export."""
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 60.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    r = await c.get(f"/darwins/{did}/ledger.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers.get("content-disposition", "")
    assert r.text.splitlines()[0].startswith("seq,timestamp_utc,type")

    r = await c.get("/darwins/does-not-exist/ledger.csv")
    assert r.status_code == 404


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


async def test_events_since_returns_only_newer_oldest_first(client) -> None:
    """`?since=seq` is the polling + WS-resync incremental cursor.

    The result must contain only events strictly newer than `since`, returned
    oldest-first so callers can append. `next_cursor` should track the head.
    """
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 50.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 3)

    head = await c.get(f"/darwins/{did}/events", params={"limit": 5})
    first_seq = head.json()["events"][-1]["seq"]  # oldest in the page

    r = await c.get(f"/darwins/{did}/events", params={"since": first_seq, "limit": 100})
    body = r.json()
    seqs = [e["seq"] for e in body["events"]]
    assert all(s > first_seq for s in seqs), seqs
    assert seqs == sorted(seqs), "since= must be oldest-first"
    if body["events"]:
        assert body["next_cursor"] == body["events"][-1]["seq"]


async def test_events_cursor_pagination_newest_first(client) -> None:
    """`?cursor=seq` returns events with seq < cursor, newest-first, and
    surfaces a `next_cursor` chained to the page's smallest seq."""
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 50.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    head = await c.get(f"/darwins/{did}/events", params={"limit": 3})
    head_body = head.json()
    assert len(head_body["events"]) > 0
    head_min_seq = head_body["events"][-1]["seq"]

    r = await c.get(f"/darwins/{did}/events", params={"cursor": head_min_seq, "limit": 5})
    body = r.json()
    seqs = [e["seq"] for e in body["events"]]
    assert all(s < head_min_seq for s in seqs), seqs
    assert seqs == sorted(seqs, reverse=True), "cursor= must be newest-first"


async def test_events_kind_filter(client) -> None:
    """`?kind=event:birth` returns only matching kinds (prefix-respecting)."""
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 50.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 2)

    r = await c.get(f"/darwins/{did}/events", params={"kind": "event:birth", "limit": 50})
    body = r.json()
    assert body["total"] >= 1
    for ev in body["events"]:
        assert ev["kind"] == "event:birth" or ev["kind"].startswith("event:birth.")


async def test_anatomy_snapshot_shape(client) -> None:
    """`/anatomy` exposes the catalog + recent activity heat. Front-end
    contract: orchestrator + neurons[] + edges[] + heatmap + dna."""
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 50.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 2)

    r = await c.get(f"/darwins/{did}/anatomy")
    assert r.status_code == 200
    body = r.json()
    assert body["darwin_id"] == did
    assert body["orchestrator"]["id"] == "orchestrator"
    assert body["orchestrator"]["state"] in {"running", "alive", "paused", "dead", "killed"}
    assert isinstance(body["neurons"], list) and len(body["neurons"]) > 0
    sample = body["neurons"][0]
    for key in ("id", "label", "category", "status"):
        assert key in sample
    assert sample["status"] in {"idle", "active", "disabled", "error"}
    # Every neuron has an edge from the orchestrator.
    edge_targets = {e["to"] for e in body["edges"]}
    neuron_ids = {n["id"] for n in body["neurons"]}
    assert neuron_ids <= edge_targets
    assert set(body["heatmap"]) <= neuron_ids
    assert "dna" in body


async def test_anatomy_disabled_neuron_status(client) -> None:
    """Disabling a neuron flips its `status` to `disabled` in the snapshot."""
    c, sup = client
    r = await c.post("/darwins", json={"opening_usdc": 50.0})
    did = r.json()["darwin_id"]
    h = sup._darwins[did]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 1)

    snap = (await c.get(f"/darwins/{did}/anatomy")).json()
    target = snap["neurons"][0]["id"]

    await c.post(
        f"/darwins/{did}/controls/disabled_neurons",
        json={"neurons": [target]},
    )
    snap2 = (await c.get(f"/darwins/{did}/anatomy")).json()
    matched = next(n for n in snap2["neurons"] if n["id"] == target)
    assert matched["status"] == "disabled"


async def test_aggregate_escalations_inbox_sorted(client) -> None:
    """`/escalations` returns owner-scoped tickets across owned Darwins."""
    c, sup = client
    a = (await c.post("/darwins", json={"opening_usdc": 50.0})).json()["darwin_id"]
    b = (await c.post("/darwins", json={"opening_usdc": 50.0})).json()["darwin_id"]
    h_a = sup._darwins[a]  # noqa: SLF001
    h_b = sup._darwins[b]  # noqa: SLF001
    await _wait_until(lambda: h_a.runtime.state.tick >= 2 and h_b.runtime.state.tick >= 2)

    r = await c.get("/escalations")
    assert r.status_code == 200
    body = r.json()
    assert "tickets" in body
    # All returned tickets must belong to one of the two darwins.
    for tk in body["tickets"]:
        assert tk["darwin_id"] in {a, b}


def test_ws_stream_initial_snapshots_and_events() -> None:
    """WS sends snapshot.{anatomy,status} on accept and forwards new
    audit events as `timeline.event` messages.

    Uses TestClient because httpx.AsyncClient doesn't support WebSockets.
    """
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)

    try:
        with TestClient(app) as c:
            spawn = c.post("/darwins", json={"opening_usdc": 50.0}).json()
            did = spawn["darwin_id"]

            # Pause the runtime so background events don't bury the inject.
            c.post(f"/darwins/{did}/pause")

            with c.websocket_connect(f"/darwins/{did}/stream") as ws:
                # Subscribe immediately so the server's 50ms read window
                # captures `since=0` before falling into the polling loop.
                ws.send_json({"type": "subscribe", "channels": ["timeline"], "since": 0})

                msg = ws.receive_json()
                assert msg["type"] == "snapshot.anatomy"
                assert msg["payload"]["darwin_id"] == did
                msg = ws.receive_json()
                assert msg["type"] == "snapshot.status"
                assert msg["payload"]["darwin_id"] == did

                # Inject a synthetic audit record and confirm WS picks it up.
                handle = sup._darwins[did]  # noqa: SLF001
                handle.stack.audit_sink.append(
                    {
                        "seq": 1_000_000,
                        "prev_hash": "0" * 64,
                        "hash": "f" * 64,
                        "ts": time.time(),
                        "kind": "event:platform.test",
                        "payload": {"injected": True},
                        "darwin_id": did,
                    }
                )

                # TestClient WS has no per-message timeout — bound by
                # message budget. Each poll tick (~200ms) can produce
                # many timeline.event messages; 200 is plenty of headroom.
                got_event = False
                seen_kinds: list[str] = []
                for _ in range(200):
                    msg = ws.receive_json()
                    if msg.get("type") == "timeline.event":
                        seen_kinds.append(msg["payload"].get("kind", "?"))
                        if msg["payload"].get("kind") == "event:platform.test":
                            got_event = True
                            break
                    elif msg.get("type") != "heartbeat":
                        seen_kinds.append(f"<{msg.get('type')}>")
                assert got_event, (
                    f"expected timeline.event for the injected record; "
                    f"seen {len(seen_kinds)} messages: {seen_kinds[-15:]}"
                )
    finally:
        import asyncio
        asyncio.run(sup.shutdown())


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
    assert body["schema"] == "darwin-platform.snapshot/2"
    assert len(body["darwins"]) == 1
    assert "alerts" in body
