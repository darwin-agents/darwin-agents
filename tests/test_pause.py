"""Owner Pause/Resume — the level-3 graded response (doc 09).

Closes gap B. Pause is **not** kill: a paused Darwin halts at the next safe
checkpoint and waits. The same life resumes from the same checkpoint, with
the same wallet and the same unbroken audit chain — there is no death, no
re-birth, no resurrection rule to violate.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.machinery.pause_switch import PauseSwitch


def _events_of(stack, kind: str):
    return [r for r in stack.audit_sink.read_all() if r["kind"] == kind]


def test_pause_halts_the_same_life_at_a_checkpoint(booted):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=5))
    tick_before = rt.state.tick
    balance_before = stack.wallet.balance_usdc()

    stack.pause.pause("owner coffee break")
    st = asyncio.run(rt.run(max_heartbeats=50))  # would run 50 if not paused

    assert st.alive is True and st.paused is True
    assert st.tick == tick_before, "no heartbeat may run while paused"
    # No side effect crossed the pause boundary.
    assert stack.wallet.balance_usdc() == pytest.approx(balance_before)
    rt.audit.verify()
    # The audit chain is anchored at the pause and reflects the owner action.
    oa = _events_of(stack, "event:owner_action")
    assert any((r.get("payload") or {}).get("kind") == "pause" for r in oa)
    # And — most importantly — the death sequence did NOT fire.
    kinds = {r["kind"] for r in stack.audit_sink.read_all()}
    assert "event:death.begin" not in kinds and "event:death.end" not in kinds


def test_resume_continues_the_same_life(booted):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=3))
    stack.pause.pause()
    asyncio.run(rt.run(max_heartbeats=10))  # halts immediately
    paused_tick = rt.state.tick

    stack.pause.resume()
    st = asyncio.run(rt.run(max_heartbeats=4))

    assert st.alive and not st.paused
    assert st.tick > paused_tick, "resume must advance the same life"
    rt.audit.verify()
    # Both ends of the pause are in the audit log, in order.
    kinds = [
        (r.get("payload") or {}).get("kind")
        for r in _events_of(stack, "event:owner_action")
    ]
    assert kinds[:2] == ["pause", "resume"]


def test_pause_does_not_silence_the_kill(booted):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=3))
    stack.pause.pause()
    asyncio.run(rt.run(max_heartbeats=1))  # parks

    # Owner pauses and kills — kill must still take effect on the next drive,
    # halting via the death sequence (kill is final; pause is not refuge).
    stack.kill.flip("operator escalated to terminate")
    st = asyncio.run(rt.run(max_heartbeats=5))

    assert st.alive is False
    rt.audit.verify()
    kinds = {r["kind"] for r in stack.audit_sink.read_all()}
    assert "event:kill.halt" in kinds


def test_pause_is_idempotent_while_held(booted):
    rt, stack = booted
    asyncio.run(rt.run(max_heartbeats=2))
    stack.pause.pause()
    asyncio.run(rt.run(max_heartbeats=2))  # logs the pause
    asyncio.run(rt.run(max_heartbeats=2))  # the flag is still set; no extra log
    asyncio.run(rt.run(max_heartbeats=2))
    pause_events = [
        r for r in _events_of(stack, "event:owner_action")
        if (r.get("payload") or {}).get("kind") == "pause"
    ]
    assert len(pause_events) == 1


def test_pause_source_contract_is_enforced():
    with pytest.raises(TypeError):
        PauseSwitch(object())  # no is_paused()/reason()
