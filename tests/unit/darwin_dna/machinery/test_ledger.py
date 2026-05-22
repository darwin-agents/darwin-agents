"""The double-entry ledger + per-heartbeat reconciliation (doc 05).

Closes the gap where the ledger/reconciliation existed only as an external
sim invariant (I7): the genome now keeps a hash-chained double-entry ledger,
reconciles it against the authoritative wallet every heartbeat, and halts
*itself* gracefully on drift.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.evolution import boot
from darwin_dna.machinery.ledger import Ledger, LedgerError
from darwin_fakes import make_platform


def test_double_entry_balance_and_hash_chain():
    led = Ledger("d")
    led.seed_opening(100.0, ts=1.0)
    led.journal_new(
        [
            {"debit": {"to": "platform.gateway", "amount_usdc": 0.5,
                       "memo": "llm:reasoning", "kind": "metered", "ts": 2.0}},
            {"credit": {"from": "affiliate_network", "amount_usdc": 4.0,
                        "ts": 3.0}},
            {"reproduction_transfer": {"amount_usdc": 10.0, "ts": 4.0}},
        ],
        now=5.0,
    )
    # 100 − 0.5 + 4 − 10
    assert led.treasury_balance() == pytest.approx(93.5)
    led.verify()  # chain intact
    # every entry is a real double-entry (distinct accounts, positive amount)
    for e in led.entries:
        assert e.debit != e.credit and e.amount_usdc > 0


def test_tampering_breaks_the_chain():
    led = Ledger("d")
    led.seed_opening(50.0, ts=1.0)
    led.journal_new(
        [{"debit": {"amount_usdc": 1.0, "memo": "compute", "kind": "metered"}}],
        now=2.0,
    )
    led._entries[1] = led._entries[1].model_copy(update={"amount_usdc": 999.0})
    with pytest.raises(LedgerError):
        led.verify()


def test_reconcile_within_tolerance_ok_else_raises():
    led = Ledger("d")
    led.seed_opening(100.0, ts=1.0)
    assert led.reconcile(100.0, tolerance=0.01) == pytest.approx(0.0)
    with pytest.raises(LedgerError, match="drift"):
        led.reconcile(80.0, tolerance=0.01)


def test_journal_cursor_is_idempotent():
    led = Ledger("d")
    led.seed_opening(10.0, ts=1.0)
    movs = [{"debit": {"amount_usdc": 1.0, "memo": "llm:fast", "kind": "metered"}}]
    assert led.journal_new(movs, now=2.0) == 1
    assert led.journal_new(movs, now=3.0) == 0  # nothing new
    assert led.treasury_balance() == pytest.approx(9.0)


def test_loop_reconciles_every_heartbeat(booted, vclock):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=20))
    rt.ledger.verify()
    # The loop journals a heartbeat's debits at the *start* of the next one,
    # so a fixed-length run always leaves the final heartbeat's spend
    # un-journaled — a bounded one-heartbeat tail, well within the genome's
    # 0.01 reconciliation tolerance (the Darwin stays healthy). Flush that
    # tail, then the ledger reconciles against the wallet exactly.
    rt.ledger.journal_new(stack.wallet.movements(), now=vclock())
    assert rt.ledger.treasury_balance() == pytest.approx(
        stack.wallet.balance_usdc(), abs=1e-6
    )
    assert rt.state.alive


def test_phantom_wallet_drift_halts_gracefully(vclock):
    platform, stack = make_platform(opening_usdc=80.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    asyncio.run(rt.run(max_heartbeats=5))

    # Money leaves the wallet with no corresponding movement in the feed —
    # exactly the divergence the heartbeat reconciliation exists to catch.
    stack.wallet._balance -= 5.0
    asyncio.run(rt.run(max_heartbeats=5))

    assert rt.state.alive is False
    assert "ledger reconciliation" in (rt.state.death_reason or "")
    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    assert "event:ledger.reconciliation_failed" in kinds
    assert {"event:death.begin", "event:death.end"} <= set(kinds)
    rt.audit.verify()  # graceful: the chain is intact through the halt
