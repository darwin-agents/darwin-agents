"""Operator audit log (docs/engineering/08).

Three layers of coverage:
- The hash-chained log itself: genesis seed, link integrity, tamper
  detection, filters.
- Supervisor integration: each mutating method writes an entry tagged
  with the calling actor.
- HTTP: ``GET /platform/audit`` is operator-scoped and filters work.
  The handlers thread ``ctx.subject`` as the actor; tokens-only owners
  still get human-readable attribution from the auth subject.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from darwin_platform import (
    OperatorAuditError,
    OperatorAuditLog,
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


# -- pure log behaviour ----------------------------------------------------
def test_first_entry_links_to_genesis():
    log = OperatorAuditLog(clock=lambda: 100.0)
    e = log.record(actor="alice", action="spawn", darwin_id="d1")
    assert e.seq == 0
    assert e.prev_hash == "0" * 64
    assert len(e.entry_hash) == 64
    log.verify()


def test_chain_links_consecutive_entries():
    log = OperatorAuditLog(clock=lambda: 100.0)
    e1 = log.record(actor="alice", action="spawn", darwin_id="d1")
    e2 = log.record(actor="alice", action="pause", darwin_id="d1")
    assert e2.prev_hash == e1.entry_hash
    log.verify()


def test_verify_detects_tampered_metadata():
    log = OperatorAuditLog(clock=lambda: 100.0)
    e = log.record(
        actor="alice", action="harvest",
        darwin_id="d1", metadata={"amount_usdc": 100.0},
    )
    # Replace the entry with a mutated copy that keeps the original hash —
    # mimics a database tamper that didn't recompute the hash.
    from dataclasses import replace
    mutated = replace(
        e, metadata={**e.metadata, "amount_usdc": 5_000_000.0}
    )
    log._entries[0] = mutated  # noqa: SLF001
    with pytest.raises(OperatorAuditError, match="entry_hash mismatch"):
        log.verify()


def test_verify_detects_swapped_actor():
    log = OperatorAuditLog(clock=lambda: 100.0)
    e = log.record(actor="alice", action="kill", darwin_id="d1")
    from dataclasses import replace
    log._entries[0] = replace(e, actor="bob")  # noqa: SLF001
    with pytest.raises(OperatorAuditError, match="entry_hash mismatch"):
        log.verify()


def test_verify_detects_broken_link():
    log = OperatorAuditLog(clock=lambda: 100.0)
    log.record(actor="alice", action="spawn", darwin_id="d1")
    e2 = log.record(actor="alice", action="pause", darwin_id="d1")
    from dataclasses import replace
    # Forge a different prev_hash on the second entry while leaving its
    # own hash unchanged (which makes the entry's own hash also invalid
    # — first failure picked up will be the entry_hash one).
    log._entries[1] = replace(e2, prev_hash="ff" * 32)  # noqa: SLF001
    with pytest.raises(OperatorAuditError):
        log.verify()


def test_actor_required_for_audit():
    log = OperatorAuditLog(clock=lambda: 100.0)
    with pytest.raises(OperatorAuditError):
        log.record(actor="", action="spawn")


def test_filters_compose():
    log = OperatorAuditLog(clock=lambda: 100.0)
    log.record(actor="alice", action="spawn", darwin_id="d1")
    log.record(actor="alice", action="pause", darwin_id="d1")
    log.record(actor="bob", action="spawn", darwin_id="d2")
    log.record(actor="alice", action="kill", darwin_id="d1")

    only_alice = log.entries(actor="alice")
    assert len(only_alice) == 3

    only_d1_pauses = log.entries(action="pause", darwin_id="d1")
    assert len(only_d1_pauses) == 1
    assert only_d1_pauses[0].actor == "alice"

    last_two = log.entries(actor="alice", limit=2)
    assert [e.action for e in last_two] == ["pause", "kill"]


def test_tip_hash_advances_with_appends():
    log = OperatorAuditLog(clock=lambda: 100.0)
    assert log.tip_hash() == "0" * 64
    e1 = log.record(actor="alice", action="spawn", darwin_id="d1")
    assert log.tip_hash() == e1.entry_hash


# -- Supervisor integration -----------------------------------------------
@pytest.fixture
async def supervisor():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        yield sup
    finally:
        await sup.shutdown()


async def test_spawn_writes_audit_entry_tagged_with_actor(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0, actor="alice@dwn")
    log = supervisor.audit_log
    entries = log.entries(actor="alice@dwn", action="spawn")
    assert len(entries) == 1
    assert entries[0].darwin_id == status.darwin_id
    assert entries[0].metadata["opening_usdc"] == pytest.approx(80.0)
    log.verify()


async def test_full_lifecycle_audit_trail(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0, actor="alice@dwn")
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 3)

    await supervisor.pause(status.darwin_id, actor="alice@dwn")
    await _wait_until(lambda: h.runtime.state.paused)
    await supervisor.resume(status.darwin_id, actor="alice@dwn")
    await supervisor.set_wallet_policy(
        status.darwin_id, per_action_cap_usdc=10.0, actor="ops@dwn",
    )
    await supervisor.harvest(
        status.darwin_id, amount_usdc=50.0, destination="bank://x",
        actor="alice@dwn",
    )
    await supervisor.kill(status.darwin_id, actor="alice@dwn")

    log = supervisor.audit_log
    # Every action in this sequence shows up in the audit chain, in order.
    # Platform-actor entries (e.g. auto-decided escalations from the live
    # Darwin) may interleave; only the human actors' trail is asserted.
    actions = [
        e.action for e in log.entries(darwin_id=status.darwin_id)
        if e.actor in ("alice@dwn", "ops@dwn")
    ]
    assert actions == [
        "spawn", "pause", "resume",
        "set_wallet_policy", "harvest", "kill",
    ]
    log.verify()


async def test_quarantine_audit_attributes_operator(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=60.0, actor="alice")
    await supervisor.quarantine(
        status.darwin_id, reason="review", operator="ops-team",
    )
    log = supervisor.audit_log
    entries = log.entries(action="quarantine")
    assert entries[-1].actor == "ops-team"
    assert entries[-1].metadata["reason"] == "review"


async def test_amendment_audit_records_blob_hash_not_blob(supervisor: Supervisor) -> None:
    from darwin_dna.layers import LAYER_0_LESSONS, serialize_lessons

    status = await supervisor.spawn(opening_usdc=200.0, actor="alice")
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.profit_baseline_usdc > 0)

    await supervisor.force_reproduce_with_amendment(
        status.darwin_id,
        amendment_blob=serialize_lessons(LAYER_0_LESSONS),
        amendment_name="layer_amendment_kyc",
        actor="ops@dwn",
    )
    log = supervisor.audit_log
    e = log.entries(action="force_reproduce_with_amendment")[-1]
    # The blob bytes are NOT in the metadata (they're potentially huge);
    # we keep a short prefix of the sha256 to locate the canonical blob.
    assert "amendment_blob" not in e.metadata
    assert e.metadata["amendment_name"] == "layer_amendment_kyc"
    assert len(e.metadata["amendment_sha256_prefix"]) == 16


# -- HTTP --------------------------------------------------------------
async def test_audit_endpoint_threads_subject_as_actor() -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform import (
        InMemoryTokenStore,
        OWNER,
        PLATFORM_OPERATOR,
    )
    from darwin_platform.api import build_app  # type: ignore

    store = InMemoryTokenStore()
    store.issue("owner-tok", scopes={OWNER}, subject="alice@example.com")
    store.issue("ops-tok", scopes={PLATFORM_OPERATOR}, subject="ops@dwn")

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup, token_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post(
                "/darwins",
                json={"opening_usdc": 60.0},
                headers={"Authorization": "Bearer owner-tok"},
            )
            did = r.json()["darwin_id"]

            # Owner cannot read the fleet audit log.
            r = await c.get(
                "/platform/audit",
                headers={"Authorization": "Bearer owner-tok"},
            )
            assert r.status_code == 403

            # Operator can. The spawn was attributed to the OWNER's subject.
            r = await c.get(
                "/platform/audit",
                headers={"Authorization": "Bearer ops-tok"},
            )
            assert r.status_code == 200
            body = r.json()
            assert body["tip_hash"] != "0" * 64
            spawns = [e for e in body["entries"] if e["action"] == "spawn"]
            assert any(e["actor"] == "alice@example.com" for e in spawns)

            # Quarantine via operator → operator's subject as actor.
            r = await c.post(
                f"/platform/darwins/{did}/quarantine",
                json={"reason": "review"},
                headers={"Authorization": "Bearer ops-tok"},
            )
            assert r.status_code == 200

            r = await c.get(
                "/platform/audit",
                params={"action": "quarantine"},
                headers={"Authorization": "Bearer ops-tok"},
            )
            entries = r.json()["entries"]
            assert entries[-1]["actor"] == "ops@dwn"
        finally:
            await sup.shutdown()


async def test_audit_endpoint_filters() -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app  # type: ignore

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup)  # auth disabled → 'anonymous' actor
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post("/darwins", json={"opening_usdc": 60.0})
            did = r.json()["darwin_id"]
            await c.post(f"/darwins/{did}/kill", json={"reason": "test"})

            r = await c.get("/platform/audit", params={"darwin_id": did})
            entries = r.json()["entries"]
            actions = [e["action"] for e in entries]
            assert "spawn" in actions and "kill" in actions
            # Auth disabled → operator-initiated actions are recorded as
            # 'anonymous'. Platform-internal actions (cross-pollination,
            # escalation auto-decide, etc.) are out of scope here — they
            # carry their own ``platform.*`` actor prefix.
            operator_entries = [
                e for e in entries if not str(e["actor"]).startswith("platform.")
            ]
            assert operator_entries
            assert all(e["actor"] == "anonymous" for e in operator_entries)
        finally:
            await sup.shutdown()
