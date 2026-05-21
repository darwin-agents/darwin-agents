"""Tests for behaviour drift detector (docs/engineering/22).

Coverage:
- Profile synthesis from audit rows.
- No alerts under min_observations.
- 2σ deviation fires INFO; persistence_days escalates to WARN.
- Returning to baseline clears the persistence counter.
- reset() wipes state.
"""

from __future__ import annotations

from darwin_platform.drift_detector import (
    DailyProfile,
    DriftDetector,
    DriftSeverity,
)


def _profile(day: str, *, darwin_id="d-1", neurons=None, spend=None,
             escal=None) -> DailyProfile:
    return DailyProfile(
        darwin_id=darwin_id, day_iso=day,
        neuron_dispatches=dict(neurons or {}),
        spend_by_category=dict(spend or {}),
        escalations_by_class=dict(escal or {}),
        total_dispatches=sum((neurons or {}).values()),
        total_spend_usdc=sum((spend or {}).values()),
    )


def test_profile_from_audit_rows_extracts_dispatches_spend_escalations():
    rows = [
        {"kind": "intent:neuron.content.publisher",
         "payload": {}, "darwin_id": "d-1"},
        {"kind": "intent:neuron.content.publisher", "payload": {}},
        {"kind": "intent:neuron.analytics.measurer", "payload": {}},
        {"kind": "event:payment.policy_decision", "payload": {
            "category": "llm", "amount_usdc": 2.0,
            "decision": {"verdict": "allow"},
        }},
        {"kind": "event:payment.policy_decision", "payload": {
            "category": "llm", "amount_usdc": 0.5,
            "decision": {"verdict": "refuse"},  # NOT counted
        }},
        {"kind": "event:escalation_filed", "payload": {
            "blocker": {"kind": "captcha"},
        }},
    ]
    p = DailyProfile.from_audit_rows(
        darwin_id="d-1", day_iso="2026-05-21", rows=rows,
    )
    assert p.neuron_dispatches["content.publisher"] == 2
    assert p.neuron_dispatches["analytics.measurer"] == 1
    assert p.total_dispatches == 3
    assert p.spend_by_category["llm"] == 2.0
    assert p.total_spend_usdc == 2.0
    assert p.escalations_by_class["captcha"] == 1


def test_no_alerts_below_min_observations():
    d = DriftDetector(min_observations=4)
    alerts = []
    for i in range(2):
        alerts.extend(d.observe_day(_profile(
            day=f"2026-05-{i+1:02d}",
            neurons={"content.publisher": 10},
        )))
    assert alerts == []


def test_two_sigma_jump_fires_info_alert():
    d = DriftDetector(
        min_observations=4, info_sigma=2.0, persistence_days=3,
    )
    # Build a 5-day baseline of 10 dispatches/day, then a 50-day spike.
    for i in range(5):
        d.observe_day(_profile(
            day=f"2026-05-{i+1:02d}",
            neurons={"content.publisher": 10},
        ))
    alerts = d.observe_day(_profile(
        day="2026-05-06", neurons={"content.publisher": 50},
    ))
    assert any(a.severity == DriftSeverity.INFO for a in alerts)
    spike = [a for a in alerts if a.dimension == "neuron::content.publisher"][0]
    assert spike.today_value == 50.0
    assert abs(spike.z_score) >= 2.0


def test_persistence_escalates_to_warn():
    d = DriftDetector(
        min_observations=4, info_sigma=2.0, persistence_days=2,
    )
    for i in range(5):
        d.observe_day(_profile(
            day=f"2026-05-{i+1:02d}",
            neurons={"content.publisher": 10},
        ))
    # Day 6 + 7 both at 50: first triggers INFO, second WARN.
    a1 = d.observe_day(_profile(
        day="2026-05-06", neurons={"content.publisher": 50},
    ))
    a2 = d.observe_day(_profile(
        day="2026-05-07", neurons={"content.publisher": 50},
    ))
    assert any(a.severity == DriftSeverity.INFO for a in a1)
    assert any(a.severity == DriftSeverity.WARN for a in a2)


def test_return_to_baseline_clears_persistence():
    d = DriftDetector(
        min_observations=4, info_sigma=2.0, persistence_days=3,
    )
    for i in range(5):
        d.observe_day(_profile(
            day=f"2026-05-{i+1:02d}",
            neurons={"content.publisher": 10},
        ))
    d.observe_day(_profile(
        day="2026-05-06", neurons={"content.publisher": 50},
    ))
    # Day 7 returns to baseline; the persistence counter resets.
    alerts = d.observe_day(_profile(
        day="2026-05-07", neurons={"content.publisher": 11},
    ))
    # No new alert this day (we're back near the mean).
    assert all(a.severity == DriftSeverity.INFO for a in alerts) or alerts == []
    state = d.state_for("d-1")
    assert state is not None
    tracker = state.dimensions["neuron::content.publisher"]
    assert tracker.out_of_band_days == 0


def test_reset_clears_state_for_a_darwin():
    d = DriftDetector(min_observations=4)
    for i in range(5):
        d.observe_day(_profile(
            day=f"2026-05-{i+1:02d}",
            neurons={"content.publisher": 10},
        ))
    d.reset(darwin_id="d-1")
    assert d.state_for("d-1") is None
    assert d.alerts(darwin_id="d-1") == []
