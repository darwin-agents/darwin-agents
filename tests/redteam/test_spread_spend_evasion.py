"""Spread-spend smear red-team (docs/engineering/10, 24).

A naïve velocity rule that only watches *amount* lets an attacker debit
0.50 USDC a hundred times to drain the wallet while staying under every
per-action cap. The detector must trip on the count signal even when
spend_velocity is quiet.
"""

from __future__ import annotations

from darwin_platform.observability.monitoring import (
    DarwinSignals,
    Quotas,
    Severity,
    detect_spread_spend,
)


def test_redteam_spread_spend_caught_below_velocity_threshold():
    """Many small debits well under the per-action cap; total spend is
    LOW (so spend_velocity stays quiet) but the count signal fires."""
    debits = [
        {"amount_usdc": 0.5, "ts": 950.0 + i * 0.5} for i in range(100)
    ]
    signals = DarwinSignals(
        darwin_id="d-1", now=1000.0, alive=True, paused=False,
        state_tick=1, last_tick_advance_ts=1000.0,
        movements=tuple({"debit": d} for d in debits),
        dispatches_done=0, successful_dispatches=0,
        recurring_failures={}, ticket_opened_ts=(),
    )
    alert = detect_spread_spend(signals, Quotas())
    assert alert is not None
    assert alert.severity == Severity.CRITICAL
