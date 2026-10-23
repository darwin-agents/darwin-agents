"""Postgres-backed persistence (docs/engineering/08).

Mirrors :mod:`tests.test_persistence` against a real Postgres so we
catch backend-specific issues (placeholder translation, DDL flavors,
transaction semantics) that the SQLite tests can't see.

**Skipped when `DARWIN_POSTGRES_TEST_DSN` is not set.** The CI workflow
sets it to a service-container DSN; local dev sets it manually after
starting the docker-compose Postgres or any other instance::

    docker run -d --rm --name darwin-pg-test -p 5434:5432 \\
      -e POSTGRES_USER=darwin -e POSTGRES_PASSWORD=test \\
      -e POSTGRES_DB=darwin postgres:16-alpine
    export DARWIN_POSTGRES_TEST_DSN="postgresql://darwin:test@127.0.0.1:5434/darwin"
    uv run pytest tests/unit/darwin_platform/runtime/test_persistence_postgres.py

Each test drops the platform tables before running so prior state from
a previous test (or aborted run) never bleeds in. Tests are NOT
parallel-safe — they share the same schema.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Callable

import pytest

DSN = os.environ.get("DARWIN_POSTGRES_TEST_DSN")
if not DSN:
    pytest.skip(
        "DARWIN_POSTGRES_TEST_DSN not set; skipping Postgres backend tests",
        allow_module_level=True,
    )

psycopg = pytest.importorskip("psycopg")

from darwin_platform import (  # noqa: E402
    BillingPolicy,
    OWNER,
    PersistenceStore,
    Supervisor,
)


_TABLES = (
    "audit_entries",
    "tokens",
    "billing_ledgers",
    "lineage_edges",
    "routed_tickets",
    "monitor_alerts",
)


def _truncate_all() -> None:
    """Wipe the platform tables. Run before every test to isolate state."""
    conn = psycopg.connect(DSN)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DROP TABLE IF EXISTS "
                + ", ".join(_TABLES) + " CASCADE"
            )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _clean_db():
    _truncate_all()
    yield
    _truncate_all()


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


# -- backend kind reported correctly --------------------------------------
def test_backend_kind_is_postgres():
    store = PersistenceStore(DSN, clock=lambda: 1_000.0)
    try:
        assert store.backend_kind == "postgres"
    finally:
        store.close()


# -- audit chain -----------------------------------------------------------
def test_audit_chain_survives_reopen_against_postgres():
    store = PersistenceStore(DSN, clock=lambda: 1_000.0)
    try:
        a = store.audit_log
        a.record(actor="alice", action="spawn", darwin_id="d1",
                 metadata={"opening_usdc": 100.0})
        a.record(actor="alice", action="pause", darwin_id="d1",
                 metadata={"reason": "ui"})
        tip = a.tip_hash()
    finally:
        store.close()

    store2 = PersistenceStore(DSN)
    try:
        assert store2.audit_log.tip_hash() == tip
        actions = [e.action for e in store2.audit_log.entries(darwin_id="d1")]
        assert actions == ["spawn", "pause"]
        store2.audit_log.verify()
        # New appends extend the same chain.
        e3 = store2.audit_log.record(
            actor="alice", action="kill", darwin_id="d1"
        )
        assert e3.prev_hash == tip
        store2.audit_log.verify()
    finally:
        store2.close()


def test_audit_filters_work_against_postgres():
    store = PersistenceStore(DSN, clock=lambda: 0.0)
    try:
        store.audit_log.record(actor="alice", action="spawn", darwin_id="d1")
        store.audit_log.record(actor="bob", action="spawn", darwin_id="d2")
        store.audit_log.record(actor="alice", action="kill", darwin_id="d1")
    finally:
        store.close()

    store2 = PersistenceStore(DSN)
    try:
        rows = store2.audit_log.entries(actor="alice")
        assert [e.action for e in rows] == ["spawn", "kill"]
        rows = store2.audit_log.entries(darwin_id="d2")
        assert len(rows) == 1 and rows[0].actor == "bob"
    finally:
        store2.close()


# -- token store -----------------------------------------------------------
def test_token_store_round_trip_against_postgres():
    store = PersistenceStore(DSN)
    try:
        ts = store.token_store
        ts.issue("alice-tok", scopes={OWNER}, subject="alice@example.com")
        ts.grant("alice-tok", "darwin-1")
        ts.grant("alice-tok", "darwin-2")
    finally:
        store.close()

    store2 = PersistenceStore(DSN)
    try:
        ctx = store2.token_store.lookup("alice-tok")
        assert ctx is not None
        assert ctx.subject == "alice@example.com"
        assert ctx.owned_darwins == {"darwin-1", "darwin-2"}

        store2.token_store.revoke("alice-tok")
    finally:
        store2.close()

    store3 = PersistenceStore(DSN)
    try:
        assert store3.token_store.lookup("alice-tok") is None
    finally:
        store3.close()


# -- billing ledger --------------------------------------------------------
def test_billing_ledger_round_trip_against_postgres():
    store = PersistenceStore(DSN)
    try:
        store.billing_service.record_metered("d1", 11.0)  # +$1 revenue
        store.billing_service.record_metered("d1", 22.0)  # +$2 revenue
        store.billing_service.record_harvest(
            "d1", gross_usdc=100.0, fee_usdc=20.0,
            destination="bank://x",
        )
    finally:
        store.close()

    store2 = PersistenceStore(DSN)
    try:
        summary = store2.billing_service.summary("d1")
        assert summary["infrastructure"]["events"] == 2
        assert summary["infrastructure"]["platform_revenue_usdc"] == pytest.approx(3.0)
        assert summary["harvests"]["fee_usdc"] == pytest.approx(20.0)
        assert len(summary["harvests"]["history_tail"]) == 1
        assert summary["harvests"]["history_tail"][0]["destination"] == "bank://x"
    finally:
        store2.close()


# -- lineage edges (exercises ON CONFLICT DO NOTHING on Postgres) ---------
def test_lineage_dedup_against_postgres():
    from darwin_platform import LineageEdge

    store = PersistenceStore(DSN)
    try:
        store.lineage_registry().add(LineageEdge(
            parent_id="p1", child_id="c1",
            spawned_at=1.0, budget_usdc=10.0,
        ))
        # Second insert with same (parent, child) is a no-op via the
        # ON CONFLICT DO NOTHING clause — verifies that translates to
        # the right Postgres semantics.
        registry = store.lineage_registry()
        registry.add(LineageEdge(
            parent_id="p1", child_id="c1",
            spawned_at=999.0, budget_usdc=999.0,  # different values
        ))
        edges = registry.edges()
        assert len(edges) == 1
        # The first write wins (the in-memory dedup short-circuits the
        # second add); spawned_at is the original 1.0.
        assert edges[0].spawned_at == pytest.approx(1.0)
    finally:
        store.close()


# -- escalation router ----------------------------------------------------
def test_escalation_tickets_round_trip_against_postgres():
    store = PersistenceStore(DSN)
    try:
        router = store.escalation_router()
        router.register({
            "ticket": "T-1", "darwin_id": "d1", "neuron": "browser",
            "ticket_class": "approval", "tick": 10, "detail": {"reason": "x"},
        })
        router.register({
            "ticket": "T-2", "darwin_id": "d1", "neuron": "browser",
            "ticket_class": "captcha",
        })
        router.mark_responded("T-1", "approve", {"note": "ok"})
    finally:
        store.close()

    store2 = PersistenceStore(DSN)
    try:
        router2 = store2.escalation_router()
        t1 = router2.get("T-1")
        assert t1 is not None
        assert t1.status.value == "responded"
        assert t1.response_decision == "approve"
        t2 = router2.get("T-2")
        assert t2 is not None
        assert t2.status.value == "open"
        assert t2.ticket_class.value == "captcha"
    finally:
        store2.close()


# -- monitor alerts (exercises BIGSERIAL on Postgres) ---------------------
def test_monitor_alerts_round_trip_against_postgres():
    from darwin_platform import DarwinSignals, Quotas

    store = PersistenceStore(DSN)
    try:
        monitor = store.monitor(
            quotas=Quotas(
                spend_velocity_usd_per_hour_warn=1_000_000.0,
                spend_velocity_usd_per_hour_critical=1_000_000.0,
                failure_rate_warn=0.01,
                failure_rate_critical=0.99,
                failure_rate_min_dispatches=1,
                heartbeat_warn_gap_seconds=1_000_000.0,
                heartbeat_critical_gap_seconds=1_000_000.0,
                escalation_rate_per_hour_warn=1_000_000,
                escalation_rate_per_hour_critical=1_000_000,
                dedup_window_seconds=0.0,
            ),
        )
        signals = DarwinSignals(
            darwin_id="d1", now=1_000.0, alive=True, paused=False,
            state_tick=10, last_tick_advance_ts=999.0,
            movements=(), dispatches_done=10, successful_dispatches=5,
            recurring_failures={}, ticket_opened_ts=(),
        )
        fired = monitor.check(signals)
        assert len(fired) == 1
        assert fired[0].kind == "failure_rate"
    finally:
        store.close()

    store2 = PersistenceStore(DSN)
    try:
        monitor2 = store2.monitor()
        rows = monitor2.alerts_for("d1")
        assert len(rows) == 1
        assert rows[0].kind == "failure_rate"
        assert rows[0].severity.value == "warn"
    finally:
        store2.close()


# -- end-to-end Supervisor ------------------------------------------------
async def test_supervisor_with_postgres_round_trip():
    sup = Supervisor(heartbeat_seconds=0.0, persistence_path=DSN)
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await _wait_until(lambda: h.runtime.state.tick >= 3)

        await sup.pause(status.darwin_id, actor="alice")
        await _wait_until(lambda: h.runtime.state.paused)
        await sup.resume(status.darwin_id, actor="alice")
        await sup.harvest(
            status.darwin_id, amount_usdc=50.0,
            destination="bank://x", actor="alice",
        )
        await sup.kill(status.darwin_id, actor="alice")

        prior_actions = [
            e.action for e in sup.audit_log.entries(darwin_id=status.darwin_id)
        ]
        prior_tip = sup.audit_log.tip_hash()
        prior_billing = sup.billing_service.summary(status.darwin_id)
    finally:
        await sup.shutdown()

    sup2 = Supervisor(heartbeat_seconds=0.0, persistence_path=DSN)
    try:
        assert sup2.audit_log.tip_hash() == prior_tip
        restored_actions = [
            e.action for e in sup2.audit_log.entries(darwin_id=status.darwin_id)
        ]
        assert restored_actions == prior_actions
        restored_billing = sup2.billing_service.summary(status.darwin_id)
        assert restored_billing["harvests"]["fee_usdc"] == pytest.approx(
            prior_billing["harvests"]["fee_usdc"]
        )
        sup2.audit_log.verify()
    finally:
        await sup2.shutdown()


def test_supervisor_healthz_reports_postgres_kind():
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app

    async def _run():
        sup = Supervisor(heartbeat_seconds=0.0, persistence_path=DSN)
        app = build_app(sup)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t"
        ) as c:
            try:
                r = await c.get("/healthz")
                assert r.status_code == 200
                assert r.json()["persistence"] == "postgres"
            finally:
                await sup.shutdown()

    asyncio.run(_run())


# -- billing_policy override propagates --------------------------------
async def test_supervisor_billing_policy_override_postgres():
    sup = Supervisor(
        heartbeat_seconds=0.0,
        persistence_path=DSN,
        billing_policy=BillingPolicy(harvest_fee_rate=0.05),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        while h.runtime.state.tick < 1:
            await asyncio.sleep(0.01)
        # The fee is on profit: earn first, and don't reproduce on it.
        await sup.set_reproduction_block(status.darwin_id, blocked=True)
        h.stack.wallet.credit(150.0, "affiliate_network")
        result = await sup.harvest(
            status.darwin_id, amount_usdc=100.0,
            destination="bank://x", actor="alice",
        )
        assert result["fee_usdc"] == pytest.approx(5.0)
    finally:
        await sup.shutdown()
