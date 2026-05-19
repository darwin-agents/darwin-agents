"""Boot guarantee + kill-mid-run integration (docs/engineering/18).

"Layer 0 must boot a Darwin into a working state." Even naive and
unprofitable, the loop runs, neurons dispatch, the ledger reconciles, the kill
switch halts, the audit chain is unbroken — on first boot, no mutations.
"""

from __future__ import annotations

import asyncio


def test_boots_into_a_working_state(booted):
    rt, stack = booted
    st = asyncio.run(rt.run(max_heartbeats=12))

    assert st.alive
    assert st.tick == 12
    assert st.content_pieces == 5  # founding strategy seeds a 5-piece library
    assert st.published_pieces == 5
    # It dispatched exactly the seeding work (5 writes + 5 publishes) and then
    # went frugal — a founding Darwin must not keep burning once the library
    # is seeded (docs/engineering/18 failure mode: burns money faster than it
    # earns). That plateau is a feature, not a stall.
    assert st.dispatches_done == 10
    assert "11:check_reproduction" in st.trail  # still looping, just idle
    rt.audit.verify()  # unbroken chain on first boot
    assert stack.checkpoint.saves > 0  # checkpointed at node boundaries

    birth = stack.audit_sink.read_all()[0]
    assert birth["kind"] == "event:birth"


def test_ledger_reconciles_to_the_wallet(booted):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=20))

    debits = sum(e["debit"]["amount_usdc"] for e in stack.wallet.ledger if "debit" in e)
    credits = sum(
        e["credit"]["amount_usdc"] for e in stack.wallet.ledger if "credit" in e
    )
    expected = 60.0 + credits - debits
    assert abs(expected - stack.wallet.balance_usdc()) < 1e-9


def test_kill_switch_halts_mid_run_gracefully(booted):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=3))
    dispatches_before = rt.state.dispatches_done

    stack.kill.flip("operator stop")
    asyncio.run(rt.run(max_heartbeats=50))  # should halt almost immediately

    assert rt.state.alive is False
    assert rt.state.halt is True
    assert "killed" in (rt.state.death_reason or "")
    # No meaningful work happened after the kill (halt before next side effect).
    assert rt.state.dispatches_done <= dispatches_before + 1

    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    assert "event:kill.halt" in kinds  # deterministic final record
    assert any(t.get("kind") == "killed" for t in stack.escalation.tickets)
    rt.audit.verify()  # chain still intact through the halt


def test_constitution_or_trust_failure_blocks_boot(vclock):
    from darwin_dna.evolution import boot
    from darwin_fakes import make_platform
    from darwin_fakes.fake_platform import SigningService

    platform, stack = make_platform(opening_usdc=50.0, clock=vclock)
    # Swap in a different signer's key: the manifest no longer verifies, so
    # boot must raise before any side-effecting line runs.
    platform.signer = SigningService()
    import pytest

    with pytest.raises(Exception):
        boot(platform, clock=vclock, sleep=vclock.sleep)
