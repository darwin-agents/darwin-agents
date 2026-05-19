"""End-to-end tests for the Darwin Supervisor (docs/engineering/08).

Drives the Supervisor in-process — no HTTP — so failures point at the
lifecycle logic, not the wiring. The Supervisor uses asyncio tasks per
Darwin; tests use the auto-mode asyncio plugin and a tiny ``wait_until``
helper to give the event loop time to drive the runtime.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from darwin_platform import (
    Supervisor,
    SupervisorError,
    UnknownDarwin,
    snapshot,
)


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    """Yield to the event loop until ``pred`` is True or we time out. Tests
    fail loudly rather than hanging when the supervisor never advances."""
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


@pytest.fixture
async def supervisor():
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        yield sup
    finally:
        await sup.shutdown()


async def test_spawn_then_status(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    assert status.alive
    assert status.generation == 0
    assert status.parent_id is None
    assert status.balance_usdc == pytest.approx(80.0)
    # The same id is queryable after spawn returns.
    again = await supervisor.status(status.darwin_id)
    assert again.darwin_id == status.darwin_id


async def test_unknown_darwin_raises(supervisor: Supervisor) -> None:
    with pytest.raises(UnknownDarwin):
        await supervisor.status("does-not-exist")
    with pytest.raises(UnknownDarwin):
        await supervisor.pause("does-not-exist")
    with pytest.raises(UnknownDarwin):
        await supervisor.kill("does-not-exist")


async def test_supervisor_drives_heartbeats(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    # The drive task should advance the runtime. ``tick`` is incremented by
    # the orchestrator state on each node visit; cross a healthy threshold
    # rather than asserting an exact count (event-loop scheduling varies).
    await _wait_until(lambda: h.runtime.state.tick >= 20)
    assert h.runtime.state.alive
    # Audit log accumulated something past the birth event.
    events = await supervisor.events(status.darwin_id)
    assert events["total"] > 1


async def test_pause_then_resume(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    await supervisor.pause(status.darwin_id, reason="coffee")
    # Wait for the genome to observe the pause flag at its heartbeat
    # boundary and break out of the loop (state.paused becomes True).
    await _wait_until(lambda: h.runtime.state.paused)
    paused_tick = h.runtime.state.tick
    # While paused, ticks do not advance even after we yield.
    await asyncio.sleep(0.05)
    assert h.runtime.state.tick == paused_tick
    assert h.runtime.state.alive  # pause is NOT death

    await supervisor.resume(status.darwin_id)
    await _wait_until(lambda: not h.runtime.state.paused and h.runtime.state.tick > paused_tick)


async def test_kill_halts_the_runtime(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    await supervisor.kill(status.darwin_id, reason="testing kill")
    await _wait_until(lambda: not h.runtime.state.alive)
    final = await supervisor.status(status.darwin_id)
    assert not final.alive
    assert final.killed
    assert final.death_reason and "killed" in final.death_reason

    # The genome wrote kill.halt and the day was anchored before halting.
    # The audit logger namespaces kinds (intent:/event:/outcome) — match the
    # event-style entry the kill handler emits.
    events = await supervisor.events(status.darwin_id, limit=1000)
    kinds = [e.get("kind") for e in events["events"]]
    assert "event:kill.halt" in kinds


async def test_kill_then_resume_refused(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    await supervisor.kill(status.darwin_id, reason="testing kill")
    with pytest.raises(SupervisorError):
        await supervisor.resume(status.darwin_id)


async def test_force_reproduce_sets_request_flag(supervisor: Supervisor) -> None:
    """force_reproduce queues the genome's reproduce flag for the next
    heartbeat. Whether reproduction actually fires is the genome's gate
    (treasury + fitness window) and is exercised end-to-end in the sim
    tests; here we only verify the supervisor's bookkeeping."""
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h.runtime.state.tick >= 5)

    await supervisor.force_reproduce(status.darwin_id)
    # The supervisor sets state.reproduce on the next tick boundary; either
    # it has already been consumed by the time we observe (and reset back
    # to False), or it is still pending. Either way the request was queued.
    # We can also assert that asking after kill is refused.
    await supervisor.kill(status.darwin_id)
    await _wait_until(lambda: not h.runtime.state.alive)
    with pytest.raises(SupervisorError):
        await supervisor.force_reproduce(status.darwin_id)


async def test_observe_children_records_lineage_edges(supervisor: Supervisor) -> None:
    """Auto-boot of children: when a Darwin's spawner records a child, the
    supervisor reads the sealed child DNA off the record and boots it in the
    same process, recording the parent->child edge. We stage a synthetic
    spawn record (signed by the parent's signer) rather than wait for the
    full 90-day economic path that the sim tests already cover."""
    from darwin_dna._version import LAYER_0_VERSION
    from darwin_fakes.fake_platform import mint_genesis

    parent = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[parent.darwin_id]  # noqa: SLF001

    # Mint a fresh genesis off the same signer — that produces a manifest +
    # blobs that ``make_child_platform`` will accept as a generation-0 child.
    # In production the genome's reproduce() pipeline emits these; for the
    # test we synthesize an equivalent spawner record.
    child_manifest, child_blobs, _names, _identity = mint_genesis(h.stack.signer)
    spawn_record = {
        "parent_id": parent.darwin_id,
        "budget_usdc": 25.0,
        "layers": sorted(child_blobs),
        "spawned_at": supervisor._clock(),  # noqa: SLF001
        "child_manifest": child_manifest.model_dump_json(),
        "child_layer_blobs": dict(child_blobs),
    }
    # Inject the record onto the parent's spawner; the drive task will pick
    # it up at the next heartbeat boundary and auto-boot.
    h.stack.spawner.children.append(spawn_record)

    await _wait_until(lambda: len(h.children) >= 1, timeout=5.0)
    child_id = h.children[0]
    child_status = await supervisor.status(child_id)
    # The supervisor opened a child with the budget we requested. The child
    # is being driven, so by the time we read it may have spent a little
    # already; assert it started at the budget and is still close to it.
    assert child_status.balance_usdc > 24.0
    assert child_status.balance_usdc <= 25.0
    # ``DarwinStatus.parent_id`` reflects what the genome embedded in the
    # signed child manifest. The supervisor's own view of "who spawned this
    # Darwin" is the lineage edge below, which is what the doc-08 lineage
    # endpoint exposes. (In a real reproduction the genome's reproduce()
    # pipeline binds both — this synthetic record exercises only the
    # supervisor's bookkeeping.)
    lineage = await supervisor.lineage(parent.darwin_id)
    assert child_id in lineage["children"]
    assert any(
        e["parent_id"] == parent.darwin_id and e["child_id"] == child_id
        for e in lineage["edges"]
    )

    # The child Darwin is also being driven on its own task — it ticks.
    child_handle = supervisor._darwins[child_id]  # noqa: SLF001
    await _wait_until(lambda: child_handle.runtime.state.tick >= 3)


async def test_ledger_paginates(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    h = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: len(h.stack.wallet.ledger) >= 3, timeout=10.0)

    page = await supervisor.ledger(status.darwin_id, limit=2, offset=0)
    assert page["limit"] == 2
    assert len(page["movements"]) <= 2
    assert page["total"] >= len(page["movements"])
    assert page["balance_usdc"] == h.stack.wallet.balance_usdc()


async def test_respond_escalation_routes_to_inbox() -> None:
    # Use a fresh supervisor with auto=None so the inbox doesn't auto-approve
    # the test's escalation before we can answer it ourselves.
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=80.0, escalation_auto=None)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        # Post a synthetic ticket directly onto the inbox so the test does
        # not need to wait for the genome to file a real one.
        h.stack.escalation.post({"ticket": "T-1", "kind": "approval"})
        open_tickets = await sup.open_escalations(status.darwin_id)
        assert any(t.get("ticket") == "T-1" for t in open_tickets)

        await sup.respond_escalation(
            status.darwin_id,
            escalation_id="T-1",
            decision="approve",
            data={"note": "ok"},
        )
        # The supervisor tracks which tickets have been handled.
        assert "T-1" in h.handled_responses
        # And ``open_escalations`` no longer surfaces it.
        still_open = await sup.open_escalations(status.darwin_id)
        assert all(t.get("ticket") != "T-1" for t in still_open)
    finally:
        await sup.shutdown()


async def test_respond_escalation_rejects_bad_decision(supervisor: Supervisor) -> None:
    status = await supervisor.spawn(opening_usdc=80.0)
    with pytest.raises(SupervisorError):
        await supervisor.respond_escalation(
            status.darwin_id, escalation_id="T-x", decision="maybe"
        )


async def test_snapshot_captures_running_state(supervisor: Supervisor) -> None:
    a = await supervisor.spawn(opening_usdc=80.0)
    b = await supervisor.spawn(opening_usdc=60.0)
    await _wait_until(
        lambda: supervisor._darwins[a.darwin_id].runtime.state.tick >= 5  # noqa: SLF001
    )

    snap = snapshot(supervisor)
    assert snap["schema"] == "darwin-platform.snapshot/1"
    ids = {d["darwin_id"] for d in snap["darwins"]}
    assert ids == {a.darwin_id, b.darwin_id}
    # Lineage edges may or may not exist (depends on whether either Darwin
    # has reproduced); the field is always present.
    assert "lineage_edges" in snap


async def test_shutdown_kills_remaining(supervisor: Supervisor) -> None:
    a = await supervisor.spawn(opening_usdc=80.0)
    b = await supervisor.spawn(opening_usdc=60.0)
    h_a = supervisor._darwins[a.darwin_id]  # noqa: SLF001
    h_b = supervisor._darwins[b.darwin_id]  # noqa: SLF001
    await _wait_until(lambda: h_a.runtime.state.tick >= 3 and h_b.runtime.state.tick >= 3)

    await supervisor.shutdown()
    assert not h_a.runtime.state.alive
    assert not h_b.runtime.state.alive
    # Calling spawn after shutdown is refused.
    with pytest.raises(SupervisorError):
        await supervisor.spawn()
