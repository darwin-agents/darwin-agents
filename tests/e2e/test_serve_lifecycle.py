"""E2E: the real ``darwin-platform serve`` process lifecycle.

Boots ``python -m darwin_platform serve`` as a subprocess on an ephemeral
port, drives a Darwin over real HTTP, and asserts a clean shutdown. Also
covers the doc-20 "kill -9 the supervisor, restart" criterion: a Darwin
spawned against a SQLite ``--db`` resumes after the process is SIGKILLed
and rebooted. The harness lives in ``tests/e2e/conftest.py``.
"""

from __future__ import annotations

import httpx
import pytest


# ---------------------------------------------------------------------------
# test 1 — spawn → run → kill against the live HTTP server
# ---------------------------------------------------------------------------
def test_serve_spawns_runs_and_kills_a_darwin(server_factory, wait_for, assert_graceful_shutdown):
    srv = server_factory().start()
    client = httpx.Client(base_url=srv.base_url, timeout=5.0)

    # healthz reports the in-memory persistence backend.
    health = client.get("/healthz").json()
    assert health["status"] == "ok"
    assert health["persistence"] == "memory"

    # spawn.
    r = client.post("/darwins", json={"opening_usdc": 100.0})
    assert r.status_code == 201, r.text
    spawned = r.json()
    did = spawned["darwin_id"]
    assert spawned["alive"] is True
    assert spawned["balance_usdc"] == pytest.approx(100.0)

    # the drive loop is a real asyncio task in the subprocess — its tick
    # counter must advance over wall-clock time without us poking it.
    def _ticked() -> bool:
        return client.get(f"/darwins/{did}").json()["tick"] > 5

    wait_for(_ticked, timeout=15.0, what="darwin tick to advance")

    # the audit chain is growing + hash-chained (first record is birth).
    events = client.get(f"/darwins/{did}/events?limit=3").json()
    assert events["total"] > 0
    assert events["events"][0]["kind"] == "event:birth"
    assert events["events"][0]["prev_hash"] == "0" * 64

    # metrics endpoint serves Prometheus text with the fleet gauge.
    metrics = client.get("/metrics").text
    assert 'darwin_platform_darwins{state="alive"} 1' in metrics

    # kill — the genome must observe the flag and die.
    assert client.post(f"/darwins/{did}/kill").status_code == 200

    def _dead() -> bool:
        return client.get(f"/darwins/{did}").json()["alive"] is False

    wait_for(_dead, timeout=10.0, what="darwin to die after kill")
    body = client.get(f"/darwins/{did}").json()
    assert body["killed"] is True

    client.close()

    # graceful SIGTERM → uvicorn runs the lifespan shutdown (which calls
    # Supervisor.shutdown()) → process exits via the signal disposition.
    rc = srv.stop(sig="term")
    assert_graceful_shutdown(srv, rc)


# ---------------------------------------------------------------------------
# test 2 — doc-20 "kill -9 the supervisor, restart" resume criterion
# ---------------------------------------------------------------------------
def test_serve_resumes_darwin_after_kill_9(
    server_factory, tmp_path, wait_for, assert_graceful_shutdown,
):
    db = str(tmp_path / "state.db")

    # boot 1: spawn a Darwin against SQLite persistence.
    srv1 = server_factory(db=db).start()
    c1 = httpx.Client(base_url=srv1.base_url, timeout=5.0)
    assert c1.get("/healthz").json()["persistence"] == "sqlite"

    spawned = c1.post("/darwins", json={"opening_usdc": 100.0}).json()
    did = spawned["darwin_id"]

    # let it run a little so the lifecycle row + a checkpoint are durable.
    wait_for(
        lambda: c1.get(f"/darwins/{did}").json()["tick"] > 5,
        timeout=15.0, what="darwin tick before crash",
    )
    events_before = c1.get(f"/darwins/{did}/events?limit=1").json()["total"]
    c1.close()

    # SIGKILL — a hard crash. No lifespan shutdown, no clean teardown.
    srv1.stop(sig="kill")

    # boot 2: a fresh process on a fresh port, same SQLite file. The
    # supervisor's _resume_from_persistence runs inside the lifespan
    # startup, so by the time /healthz answers the Darwin is back.
    srv2 = server_factory(db=db).start()
    c2 = httpx.Client(base_url=srv2.base_url, timeout=5.0)

    darwins = c2.get("/darwins").json()["darwins"]
    assert did in darwins, (
        f"darwin {did} did not resume after restart; "
        f"fleet={darwins}; log:\n{srv2.log_tail()}"
    )

    status = c2.get(f"/darwins/{did}").json()
    assert status["alive"] is True
    assert status["killed"] is False

    # the audit chain survived the crash — it carries at least what was
    # written before the kill, and the chain still verifies (the genome's
    # AuditLogger re-verifies from durable storage at boot; a broken chain
    # would have aborted the resume).
    events_after = c2.get(f"/darwins/{did}/events?limit=1").json()["total"]
    assert events_after >= events_before

    # and it keeps running — the resumed drive loop advances the tick.
    tick_now = c2.get(f"/darwins/{did}").json()["tick"]
    wait_for(
        lambda: c2.get(f"/darwins/{did}").json()["tick"] > tick_now,
        timeout=15.0, what="resumed darwin to keep ticking",
    )

    c2.close()
    rc = srv2.stop(sig="term")
    assert_graceful_shutdown(srv2, rc)


# ---------------------------------------------------------------------------
# test 3 — Phase 1/2 wiring flags are accepted by the real serve process
# ---------------------------------------------------------------------------
def test_serve_accepts_wiring_flags(server_factory, wait_for, assert_graceful_shutdown):
    """Boots the real process with the OTLP + Phase-2 wiring flags and
    confirms it comes up healthy and the OTLP exporter is installed.

    The OTLP install sets a process-global TracerProvider, so it can
    only be exercised out-of-process like this; the endpoint can be
    unreachable (the BatchSpanProcessor exports in the background and
    swallows connection errors) — what we assert is that `serve`
    accepted the flag and logged the exporter wiring."""
    srv = server_factory(
        extra_args=[
            "--otlp-endpoint", "http://127.0.0.1:4318",
            "--enable-drift-detection",
            "--enable-merkle-anchor",
            "--kyc-provider", "stub",
            "--maintenance-interval", "2",
        ]
    ).start()
    client = httpx.Client(base_url=srv.base_url, timeout=5.0)

    # Came up healthy with all the wiring flags applied.
    assert client.get("/healthz").json()["status"] == "ok"

    # KYC is wired (--kyc-provider stub) → spawn is gated until the owner
    # completes KYC. Drive the real /platform/kyc/* flow for the auth-disabled
    # "anonymous" subject, then spawn succeeds.
    inq = client.post(
        "/platform/kyc/start", json={"subject": "anonymous"}
    ).json()["inquiry_id"]
    verdict = client.post(
        "/platform/kyc/webhook",
        json={"subject": "anonymous", "inquiry_id": inq},
    ).json()
    assert verdict["status"] == "approved"

    # A Darwin now spawns + runs with the Phase-2 services wired.
    did = client.post("/darwins", json={"opening_usdc": 100.0}).json()[
        "darwin_id"
    ]
    wait_for(
        lambda: client.get(f"/darwins/{did}").json()["tick"] > 5,
        timeout=15.0, what="darwin tick under wiring flags",
    )
    client.close()

    rc = srv.stop(sig="term")
    assert_graceful_shutdown(srv, rc)

    # The OTLP exporter wiring logged its startup line.
    log = srv.log_tail(lines=200)
    assert "tracing: exporting spans to" in log, (
        f"OTLP wiring did not log; log:\n{log}"
    )
