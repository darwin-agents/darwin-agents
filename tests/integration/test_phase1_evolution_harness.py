"""Smoke test for the Phase 1 evolution-test harness (docs/engineering/21).

Runs ``tools.evolution_test.run_evolution`` end-to-end at a small
heartbeat count and asserts the criteria report has the expected
shape. The actual long-form run lives in ``tools/evolution_test/``.
"""

from __future__ import annotations

import pytest

from tools.evolution_test.run_evolution import run_evolution


@pytest.mark.asyncio
async def test_harness_runs_and_produces_criteria():
    report = await run_evolution(
        heartbeats=220, opening_usdc=5000.0,
        require_reproduction=False, require_revenue=False,
    )
    assert report.ok is True, [c.detail for c in report.criteria if not c.ok]
    # The pipeline must have fired the propose node at least once and
    # produced an eval verdict with via='staging'.
    assert report.propose_count >= 1
    eval_criterion = next(
        c for c in report.criteria if c.name == "eval_verdict_recorded"
    )
    assert eval_criterion.ok
    assert "via=staging" in eval_criterion.detail


@pytest.mark.asyncio
async def test_harness_required_reproduction_fails_when_absent():
    """With ``require_reproduction=True`` the criterion fails (since the
    in-process loop at 220 heartbeats does not yet reproduce). This
    confirms the harness reports the gap rather than silently passing."""
    report = await run_evolution(
        heartbeats=220, opening_usdc=5000.0,
        require_reproduction=True, require_revenue=False,
    )
    assert report.ok is False
    repro = next(
        c for c in report.criteria if c.name == "reproduction_occurred"
    )
    assert repro.ok is False
