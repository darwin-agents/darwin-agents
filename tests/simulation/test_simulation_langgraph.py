"""The LangGraph engine through the simulation (docs/engineering/12, 18).

The internal runner is the offline default; LangGraph is the production
wiring. "Layer 0 done" must hold on the engine that actually ships, and the
two engines must reach the *same* outcome — the node functions are
framework-agnostic by design, so any divergence is a bug.

``run_simulation`` asserts invariants I1–I8 every simulated day and the
scenario's own expectation at the end, so reaching the asserts below already
means no invariant was violated en route on the LangGraph engine.
"""

from __future__ import annotations

import pytest

from tools.sim.run_sim import run_simulation

pytest.importorskip("langgraph")


@pytest.mark.slow
def test_layer0_done_bar_and_engine_parity_90_days():
    """One pass covers both: the done-bar must hold on LangGraph (the
    production wiring) AND LangGraph must match the internal runner exactly.
    Run each engine once; assert the bar on LangGraph and parity across.

    This is the canonical 90-day "Layer 0 done" assertion (cross-engine).
    ~3.5 min: marked slow; runs by default, skip with -m 'not slow'."""
    a = run_simulation("default", days=90, engine="internal")
    b = run_simulation("default", days=90, engine="langgraph")

    # Done-bar on the engine that actually ships.
    assert b.engine == "langgraph"
    assert b.alive is True
    assert b.reproductions >= 1
    assert b.invariants_ok is True

    # Framework-agnostic nodes ⇒ bit-for-bit identical economic outcome.
    assert a.alive == b.alive
    assert a.final_balance_usdc == b.final_balance_usdc
    assert a.published_pieces == b.published_pieces
    assert a.reproductions == b.reproductions
    assert a.fitness == b.fitness


def test_langgraph_thin_world_still_dies_gracefully():
    r = run_simulation("harsh", days=30, engine="langgraph")
    assert r.alive is False
    assert r.death_reason and "bankruptcy" in r.death_reason
