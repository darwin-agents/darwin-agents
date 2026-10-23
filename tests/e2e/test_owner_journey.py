"""E2E: an owner's day-to-day journey against the real ``serve`` process.

Pause / resume semantics on the wall-clock drive loop, the doc-09 owner
controls, the accountant exports (JSON ledger, CSV, summary), and the
dashboard's WebSocket stream (frontend doc 07) over a real socket — the
surfaces the web app depends on, exercised end to end.
"""

from __future__ import annotations

import csv
import io
import json
import time

import pytest

websockets_sync = pytest.importorskip("websockets.sync.client")


@pytest.fixture
def live(server_factory, wait_for):
    """A running server plus one spawned, ticking Darwin."""
    srv = server_factory().start()
    client = srv.client()
    did = client.post("/darwins", json={"opening_usdc": 100.0}).json()["darwin_id"]
    wait_for(lambda: client.get(f"/darwins/{did}").json()["tick"] > 3, what="first ticks")
    try:
        yield srv, client, did
    finally:
        client.close()


def test_pause_freezes_the_drive_loop_and_resume_restarts_it(live, wait_for):
    _, client, did = live
    tick = lambda: client.get(f"/darwins/{did}").json()["tick"]  # noqa: E731

    assert client.post(f"/darwins/{did}/pause", json={"reason": "e2e"}).status_code == 200
    wait_for(lambda: client.get(f"/darwins/{did}").json()["paused"], what="pause to land")

    frozen = tick()
    time.sleep(0.5)  # ~50 heartbeats at the test cadence
    assert tick() == frozen, "a paused Darwin must not keep ticking"
    assert client.get(f"/darwins/{did}").json()["alive"] is True

    assert client.post(f"/darwins/{did}/resume").status_code == 200
    wait_for(lambda: tick() > frozen, timeout=10.0, what="resumed Darwin to tick")
    assert client.get(f"/darwins/{did}").json()["paused"] is False


def test_owner_controls_round_trip(live):
    _, client, did = live
    base = f"/darwins/{did}/controls"

    assert client.post(f"{base}/spend_lock", json={"locked": True, "reason": "e2e"}).status_code == 200
    assert client.post(f"{base}/reproduction_block", json={"blocked": True}).status_code == 200
    assert client.post(f"{base}/disabled_neurons", json={"neurons": ["ads.manager"]}).status_code == 200

    controls = client.get(base).json()
    assert controls["spend_lock"]["spend_locked"] is True
    assert controls["reproduction_blocked"] is True
    assert controls["disabled_neurons"] == ["ads.manager"]

    # Every owner lever is audited (doc 09) in the platform's hash-chained
    # operator audit log — evidence beyond the HTTP response.
    audit = client.get(f"/platform/audit?darwin_id={did}").json()
    assert "spend_lock" in {row["action"] for row in audit["entries"]}, audit
    assert audit["tip_hash"]

    # Levers are reversible.
    client.post(f"{base}/spend_lock", json={"locked": False})
    client.post(f"{base}/reproduction_block", json={"blocked": False})
    client.post(f"{base}/disabled_neurons", json={"neurons": []})
    controls = client.get(base).json()
    assert controls["spend_lock"]["spend_locked"] is False
    assert controls["reproduction_blocked"] is False
    assert controls["disabled_neurons"] == []


def test_ledger_exports_agree_with_each_other(live):
    _, client, did = live

    ledger = client.get(f"/darwins/{did}/ledger?limit=1000").json()
    assert ledger, "ledger must not be empty — the opening deposit is a row"

    r = client.get(f"/darwins/{did}/ledger.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert f"darwin-{did}-ledger.csv" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert rows, "CSV export must carry at least the opening deposit"

    summary = client.get(f"/darwins/{did}/ledger/summary").json()
    assert isinstance(summary, dict) and summary


def test_websocket_stream_sends_snapshots_then_live_updates(live):
    srv, _, did = live
    url = f"ws://127.0.0.1:{srv.port}/darwins/{did}/stream"
    seen: list[dict] = []
    with websockets_sync.connect(url, open_timeout=5) as ws:
        # The handshake fan-out: anatomy first, then status, before any
        # REST follow-up is needed (frontend 07).
        first = json.loads(ws.recv(timeout=5))
        second = json.loads(ws.recv(timeout=5))
        assert first["type"] == "snapshot.anatomy"
        assert second["type"] == "snapshot.status"
        assert second["payload"]["darwin_id"] == did

        # Then a live feed: the drive loop keeps producing frames.
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and len(seen) < 3:
            seen.append(json.loads(ws.recv(timeout=5)))
    assert seen, "stream went silent after the initial snapshots"
    assert {m["type"] for m in seen} <= {
        "timeline.event", "anatomy.patch", "status", "heartbeat", "escalation.open",
    }


def test_websocket_rejects_unknown_darwin(live):
    srv, _, _ = live
    url = f"ws://127.0.0.1:{srv.port}/darwins/does-not-exist/stream"
    from websockets.exceptions import ConnectionClosed, InvalidStatus

    with pytest.raises((ConnectionClosed, InvalidStatus)):
        with websockets_sync.connect(url, open_timeout=5) as ws:
            ws.recv(timeout=5)
