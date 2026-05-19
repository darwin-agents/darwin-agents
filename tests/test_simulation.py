"""The simulation tier (docs/engineering/18, "What 'Layer 0 done' means").

The headline invariant: a Darwin runs for 90 simulated days, survives, makes
profit, reproduces once, and the audit log + ledger reconcile to zero
discrepancy. Plus: a thin world kills it *gracefully*, and a captcha-heavy
world exercises escalate-don't-fail without killing it.

``run_simulation`` asserts I1–I8 itself every simulated day and raises
``InvariantError`` on any breach, so reaching the asserts below already means
no invariant was violated en route.
"""

from __future__ import annotations

from tools.sim.run_sim import run_simulation


def test_layer0_done_bar_90_days_default():
    r = run_simulation("default", days=90)
    assert r.alive is True
    assert r.final_balance_usdc > 0
    assert r.reproductions >= 1
    assert r.invariants_ok is True


def test_thin_world_dies_gracefully():
    r = run_simulation("harsh", days=30)
    assert r.alive is False
    assert r.death_reason and "bankruptcy" in r.death_reason
    # I1/I8 were asserted inside run_simulation every day up to death.


def test_captcha_storm_survives_via_escalation():
    r = run_simulation("captcha_storm", days=20)
    assert r.alive is True
