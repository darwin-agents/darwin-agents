"""The simulation tier (docs/engineering/18, "What 'Layer 0 done' means").

The headline 90-day done-bar is asserted *across both engines* in
``test_simulation_langgraph.py`` (it runs the internal engine through the
exact same ``run_simulation`` path, then LangGraph, then parity) — that test
strictly supersedes a standalone internal-only done-bar, so it lives there.

This file keeps the fast scenario coverage: a thin world must kill the Darwin
*gracefully*, and a captcha-heavy world must exercise escalate-don't-fail
without killing it.

``run_simulation`` asserts I1–I8 itself every simulated day and raises
``InvariantError`` on any breach, so reaching the asserts below already means
no invariant was violated en route.
"""

from __future__ import annotations

from tools.sim.run_sim import run_simulation


def test_thin_world_dies_gracefully():
    r = run_simulation("harsh", days=30)
    assert r.alive is False
    assert r.death_reason and "bankruptcy" in r.death_reason
    # I1/I8 were asserted inside run_simulation every day up to death.


def test_captcha_storm_survives_via_escalation():
    r = run_simulation("captcha_storm", days=20)
    assert r.alive is True
