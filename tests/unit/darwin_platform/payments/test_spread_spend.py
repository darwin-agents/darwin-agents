"""Spread-spend smear anomaly detector (doc 10).

A Darwin (or a prompt-injection attack) trying to dodge the per-action
cap by issuing many small debits below the floor must show up as a
detector hit even if the total spend doesn't trip spend_velocity."""

from __future__ import annotations


from darwin_platform.observability.monitoring import (
    DarwinSignals,
    Quotas,
    Severity,
    detect_spread_spend,
)


def _signals(*, now=1000.0, debits):
    return DarwinSignals(
        darwin_id="d-1",
        now=now,
        alive=True,
        paused=False,
        state_tick=1,
        last_tick_advance_ts=now,
        movements=tuple({"debit": d} for d in debits),
        dispatches_done=0,
        successful_dispatches=0,
        recurring_failures={},
        ticket_opened_ts=(),
    )


def test_clean_pattern_no_alert():
    q = Quotas()
    s = _signals(debits=[
        {"amount_usdc": 5.0, "ts": 900.0},  # above floor — ignored
        {"amount_usdc": 10.0, "ts": 950.0},
    ])
    assert detect_spread_spend(s, q) is None


def test_few_small_debits_no_alert():
    q = Quotas()  # warn=30/hr, critical=80/hr
    s = _signals(debits=[
        {"amount_usdc": 0.5, "ts": 950.0 + i} for i in range(10)
    ])
    assert detect_spread_spend(s, q) is None


def test_warn_threshold():
    q = Quotas()
    s = _signals(debits=[
        {"amount_usdc": 0.5, "ts": 950.0 + i} for i in range(35)
    ])
    alert = detect_spread_spend(s, q)
    assert alert is not None
    assert alert.severity == Severity.WARN
    assert alert.kind == "spread_spend"
    assert alert.detail["small_debits_per_hour"] == 35


def test_critical_threshold():
    q = Quotas()
    s = _signals(debits=[
        {"amount_usdc": 0.5, "ts": 950.0 + i * 0.5} for i in range(100)
    ])
    alert = detect_spread_spend(s, q)
    assert alert is not None
    assert alert.severity == Severity.CRITICAL
    assert alert.suggested_level == 3
    assert alert.detail["small_debits_per_hour"] == 100


def test_only_counts_debits_in_last_hour():
    q = Quotas()
    # 50 small debits, but most are >1hr old
    debits = [
        {"amount_usdc": 0.5, "ts": 1000.0 - 7200 + i} for i in range(40)
    ] + [
        {"amount_usdc": 0.5, "ts": 990.0 + i} for i in range(10)
    ]
    s = _signals(debits=debits)
    # Only 10 in the past hour → below WARN.
    assert detect_spread_spend(s, q) is None


def test_only_counts_below_floor():
    """A few big debits should NOT count as spread-spend even if frequent."""
    q = Quotas()
    s = _signals(debits=[
        {"amount_usdc": 50.0, "ts": 950.0 + i} for i in range(50)
    ])
    assert detect_spread_spend(s, q) is None


def test_monitor_dispatches_alert():
    """End-to-end: Monitor.check() with spread-spend signals fires."""
    from darwin_platform.observability.monitoring import Monitor

    q = Quotas()
    m = Monitor(quotas=q, clock=lambda: 1000.0)
    s = _signals(debits=[
        {"amount_usdc": 0.5, "ts": 950.0 + i * 0.5} for i in range(100)
    ])
    fired = m.check(s)
    kinds = {a.kind for a in fired}
    assert "spread_spend" in kinds
