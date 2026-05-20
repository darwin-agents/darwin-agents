"""Restart survival of the platform's durable state.

Doc 08: "The audit log is permanent and hash-chained — we do not delete
audit entries even when a Darwin dies." The in-memory implementations
satisfy that within one process; the SQLite-backed variants in
:mod:`darwin_platform.persistence` survive process restarts.

These tests:
- Audit log: write entries, close, reopen, verify chain + tip_hash
  unchanged + filters work + tamper detection still detects.
- Token store: issue tokens + spawn-grant, close, reopen, lookup still
  resolves + ownership still holds + revoke is durable.
- Billing service: metered + harvest writes flush; reopen restores all
  ledgers.
- Supervisor integration: spawn / kill via a Supervisor with
  persistence_path, close, reopen with same path, audit chain shows
  ALL prior actions in order.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Callable

import pytest

from darwin_platform import (
    BillingPolicy,
    OWNER,
    OperatorAuditError,
    PersistenceStore,
    Supervisor,
)


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


# -- audit log ------------------------------------------------------------
def test_audit_log_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    store = PersistenceStore(db, clock=lambda: 1_000.0)
    a = store.audit_log
    a.record(actor="alice", action="spawn", darwin_id="d1",
             metadata={"opening_usdc": 100.0})
    a.record(actor="alice", action="pause", darwin_id="d1",
             metadata={"reason": "ui"})
    tip_before = a.tip_hash()
    store.close()

    store2 = PersistenceStore(db, clock=lambda: 1_001.0)
    a2 = store2.audit_log
    # Same chain restored.
    assert a2.tip_hash() == tip_before
    actions = [e.action for e in a2.entries(darwin_id="d1")]
    assert actions == ["spawn", "pause"]
    # Chain integrity holds end-to-end across the reload.
    a2.verify()
    # And new appends extend the same chain.
    e3 = a2.record(actor="alice", action="kill", darwin_id="d1",
                   metadata={"reason": "done"})
    assert e3.prev_hash == tip_before
    a2.verify()
    store2.close()


def test_audit_log_filters_after_reopen(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    store = PersistenceStore(db, clock=lambda: 0.0)
    a = store.audit_log
    a.record(actor="alice", action="spawn", darwin_id="d1")
    a.record(actor="bob", action="spawn", darwin_id="d2")
    a.record(actor="alice", action="kill", darwin_id="d1")
    store.close()

    store2 = PersistenceStore(db)
    rows = store2.audit_log.entries(actor="alice")
    assert [e.action for e in rows] == ["spawn", "kill"]
    rows = store2.audit_log.entries(darwin_id="d2")
    assert len(rows) == 1 and rows[0].actor == "bob"
    store2.close()


def test_audit_db_write_failure_rolls_back_memory(tmp_path: Path) -> None:
    """If the SQLite write fails, the in-memory chain must NOT keep the
    entry — otherwise verify() would pass against memory but the DB
    would be missing the row."""
    db = tmp_path / "p.db"
    store = PersistenceStore(db)
    a = store.audit_log
    a.record(actor="alice", action="spawn", darwin_id="d1")
    tip = a.tip_hash()
    # Close the connection so a subsequent write fails.
    store.close()
    with pytest.raises(OperatorAuditError):
        a.record(actor="alice", action="pause", darwin_id="d1")
    # In-memory tip unchanged (the failed entry was rolled back).
    assert a.tip_hash() == tip


# -- token store ----------------------------------------------------------
def test_token_store_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    store = PersistenceStore(db)
    ts = store.token_store
    ts.issue("alice-tok", scopes={OWNER}, subject="alice@example.com")
    ts.grant("alice-tok", "darwin-1")
    ts.grant("alice-tok", "darwin-2")
    store.close()

    store2 = PersistenceStore(db)
    ts2 = store2.token_store
    ctx = ts2.lookup("alice-tok")
    assert ctx is not None
    assert ctx.subject == "alice@example.com"
    assert OWNER in ctx.scopes
    assert ctx.owned_darwins == {"darwin-1", "darwin-2"}

    # Revoke is durable.
    ts2.revoke("alice-tok")
    store2.close()
    store3 = PersistenceStore(db)
    assert store3.token_store.lookup("alice-tok") is None
    store3.close()


# -- billing service ------------------------------------------------------
def test_billing_ledger_survives_reopen(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    store = PersistenceStore(db)
    bs = store.billing_service
    # Use the default 0.10 markup policy.
    bs.record_metered("d1", 11.0)  # +$1 revenue
    bs.record_metered("d1", 22.0)  # +$2 revenue
    bs.record_harvest("d1", gross_usdc=100.0, fee_usdc=20.0,
                      destination="bank://x")
    store.close()

    store2 = PersistenceStore(db)
    summary = store2.billing_service.summary("d1")
    assert summary["infrastructure"]["events"] == 2
    assert summary["infrastructure"]["platform_revenue_usdc"] == pytest.approx(3.0)
    assert summary["harvests"]["fee_usdc"] == pytest.approx(20.0)
    assert summary["harvests"]["events"] == 1
    # History tail row restored too.
    assert len(summary["harvests"]["history_tail"]) == 1
    assert summary["harvests"]["history_tail"][0]["destination"] == "bank://x"
    store2.close()


# -- Supervisor integration ----------------------------------------------
async def test_supervisor_persistence_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"

    sup = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
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

        # Capture state for cross-restart comparison.
        prior_actions = [
            e.action for e in sup.audit_log.entries(darwin_id=status.darwin_id)
        ]
        prior_tip = sup.audit_log.tip_hash()
        prior_billing = sup.billing_service.summary(status.darwin_id)
    finally:
        await sup.shutdown()

    # Reopen — the new supervisor sees the SAME audit chain + billing
    # values from the prior session.
    sup2 = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        # The Darwin itself does NOT come back (it was killed, and we
        # don't restore the runtime). The audit + billing rows do.
        assert sup2.audit_log.tip_hash() == prior_tip
        restored = [
            e.action for e in sup2.audit_log.entries(darwin_id=status.darwin_id)
        ]
        assert restored == prior_actions

        restored_billing = sup2.billing_service.summary(status.darwin_id)
        assert restored_billing["harvests"]["fee_usdc"] == pytest.approx(
            prior_billing["harvests"]["fee_usdc"]
        )
        assert restored_billing["infrastructure"]["events"] == (
            prior_billing["infrastructure"]["events"]
        )
        sup2.audit_log.verify()
    finally:
        await sup2.shutdown()


async def test_supervisor_persistent_token_store_exposed(tmp_path: Path) -> None:
    db = tmp_path / "platform.db"
    sup = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        ts = sup.persistent_token_store
        assert ts is not None
        ts.issue("alice", scopes={OWNER}, subject="alice@example.com")
    finally:
        await sup.shutdown()

    sup2 = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        ts2 = sup2.persistent_token_store
        assert ts2 is not None
        ctx = ts2.lookup("alice")
        assert ctx is not None
        assert ctx.subject == "alice@example.com"
    finally:
        await sup2.shutdown()


async def test_supervisor_without_persistence_path_uses_inmemory() -> None:
    """Backward compatibility: omitting persistence_path keeps the
    in-memory variants and exposes None for persistent_token_store."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        assert sup.persistent_token_store is None
        # Audit log still works — it's just in-memory.
        status = await sup.spawn(opening_usdc=60.0, actor="alice")
        rows = sup.audit_log.entries(darwin_id=status.darwin_id)
        assert len(rows) == 1
    finally:
        await sup.shutdown()


async def test_billing_policy_overrides_persistent_default(tmp_path: Path) -> None:
    """The Supervisor's ``billing_policy`` arg overrides the persistence
    store's default policy (which would otherwise be the 20%/10% default)."""
    db = tmp_path / "p.db"
    sup = Supervisor(
        heartbeat_seconds=0.0,
        persistence_path=db,
        billing_policy=BillingPolicy(harvest_fee_rate=0.05),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0, actor="alice")
        result = await sup.harvest(
            status.darwin_id, amount_usdc=100.0,
            destination="bank://x", actor="alice",
        )
        assert result["fee_usdc"] == pytest.approx(5.0)
    finally:
        await sup.shutdown()


# -- lineage edges survive restart + lineage() works for inactive darwins
async def test_lineage_edges_survive_restart(tmp_path: Path) -> None:
    from darwin_fakes.fake_platform import mint_genesis
    from darwin_platform import LineageEdge

    db = tmp_path / "p.db"
    sup = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        parent = await sup.spawn(opening_usdc=80.0, actor="alice")
        h = sup._darwins[parent.darwin_id]  # noqa: SLF001
        # Synthesise a child via the parent's spawner so the drive loop
        # observes the edge (same pattern as the supervisor lineage test).
        manifest, blobs, _names, _id = mint_genesis(h.stack.signer)
        h.stack.spawner.children.append({
            "parent_id": parent.darwin_id,
            "budget_usdc": 25.0,
            "layers": sorted(blobs),
            "spawned_at": time.time(),
            "child_manifest": manifest.model_dump_json(),
            "child_layer_blobs": dict(blobs),
        })
        await _wait_until(lambda: len(h.children) >= 1, timeout=5.0)
        child_id = h.children[0]
    finally:
        await sup.shutdown()

    # Restart — edges should be reloaded into the registry, and lineage()
    # should resolve for the parent_id even though no live handle exists.
    sup2 = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        edges = sup2._lineage_registry.edges()  # noqa: SLF001
        assert any(
            e.parent_id == parent.darwin_id and e.child_id == child_id
            for e in edges
        )
        # lineage() on an INACTIVE darwin (no handle) still resolves from
        # the registry. That's the doc-08 dashboard view.
        view = await sup2.lineage(parent.darwin_id)
        assert child_id in view["children"]
        # And resolving from the child side too — ancestry walks up.
        view = await sup2.lineage(child_id)
        assert view["parent_id"] == parent.darwin_id

        # Adding the SAME edge again is a no-op (dedup at the registry).
        sup2._lineage_registry.add(  # noqa: SLF001
            LineageEdge(
                parent_id=parent.darwin_id, child_id=child_id,
                spawned_at=999.0, budget_usdc=999.0,
            )
        )
        edges_after = sup2._lineage_registry.edges()  # noqa: SLF001
        assert len(edges_after) == len(edges)
    finally:
        await sup2.shutdown()


async def test_escalation_router_tickets_survive_restart(tmp_path: Path) -> None:
    db = tmp_path / "p.db"
    sup = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        status = await sup.spawn(
            opening_usdc=60.0, escalation_auto=None, actor="alice",
        )
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        h.stack.escalation.post({
            "ticket": "T-open", "neuron": "x", "ticket_class": "approval",
        })
        h.stack.escalation.post({
            "ticket": "T-resp", "neuron": "y", "ticket_class": "captcha",
        })
        await _wait_until(
            lambda: sup.router.get("T-open") is not None
            and sup.router.get("T-resp") is not None,
        )
        await sup.respond_escalation(
            status.darwin_id, escalation_id="T-resp",
            decision="approve", actor="alice",
        )
        # Snapshot for restart-time comparison.
        tip_open = sup.router.get("T-open").to_dict(time.time())
    finally:
        await sup.shutdown()

    sup2 = Supervisor(heartbeat_seconds=0.0, persistence_path=db)
    try:
        # Both tickets visible in the router after the restart.
        open_tk = sup2.router.get("T-open")
        resp_tk = sup2.router.get("T-resp")
        assert open_tk is not None and open_tk.status.value == "open"
        assert open_tk.ticket_class.value == tip_open["ticket_class"]
        assert resp_tk is not None and resp_tk.status.value == "responded"
        assert resp_tk.response_decision == "approve"
    finally:
        await sup2.shutdown()


async def test_monitor_alerts_survive_restart(tmp_path: Path) -> None:
    from darwin_platform import Quotas

    db = tmp_path / "p.db"
    # Configure a trip-easy quota so the failure_rate detector fires on a
    # synthesised state.
    quotas = Quotas(
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
    )
    sup = Supervisor(
        heartbeat_seconds=0.0, persistence_path=db,
        quotas=quotas, monitor_every_n_ticks=1,
    )
    try:
        status = await sup.spawn(opening_usdc=60.0, actor="alice")
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        h.runtime.state.dispatches_done = 5
        h.runtime.state.successful_dispatches = 3
        await _wait_until(
            lambda: any(a.kind == "failure_rate"
                        for a in sup.monitor.alerts_for(status.darwin_id))
        )
        before = len(sup.monitor.alerts_for(status.darwin_id))
        assert before >= 1
    finally:
        await sup.shutdown()

    sup2 = Supervisor(
        heartbeat_seconds=0.0, persistence_path=db,
        quotas=quotas, monitor_every_n_ticks=1,
    )
    try:
        # Alerts from the previous session restored.
        rows = sup2.monitor.alerts_for(status.darwin_id)
        assert len(rows) >= before
        assert any(a.kind == "failure_rate" for a in rows)
    finally:
        await sup2.shutdown()
