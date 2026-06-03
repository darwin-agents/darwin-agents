"""Resource-consumption anomaly detector (doc 22 §Anomaly detection; doc 09).

The detector flags a Darwin burning far more infrastructure than its own
baseline — upward-only, with WARN/CRITICAL severities the supervisor can route
to its auto-throttle. These tests pin the statistics: a quiet baseline, then a
spike fires; a sustained spike escalates to CRITICAL; under-consumption never
alerts; a noisy/young profile is not flagged.
"""

from __future__ import annotations

from darwin_platform.observability.resource_anomaly import (
    ResourceAnomalyDetector,
    ResourceSample,
    ResourceSeverity,
)


def _det(**kw):
    kw.setdefault("clock", lambda: 1000.0)
    return ResourceAnomalyDetector(**kw)


def _feed(det, darwin_id, dim, series):
    """Feed a per-period series for one dimension; return alerts from the last."""
    alerts = []
    for i, v in enumerate(series):
        s = ResourceSample(darwin_id=darwin_id, period_iso=f"p{i}", **{dim: v})
        alerts = det.observe(s)
    return alerts


def test_quiet_baseline_then_spike_fires_warn_or_critical():
    det = _det()
    # 5 quiet periods establish a baseline, then a big spike.
    alerts = _feed(det, "d1", "llm_spend_usdc", [1.0, 1.1, 0.9, 1.0, 1.0, 12.0])
    assert alerts, "a 12x spike over a ~1.0 baseline must alert"
    a = alerts[0]
    assert a.dimension == "llm_spend_usdc"
    assert a.severity in (ResourceSeverity.WARN, ResourceSeverity.CRITICAL)
    assert a.z_score > 0


def test_under_min_observations_never_alerts():
    det = _det(min_observations=4)
    # Only 3 periods — even with a wild value, too little history to flag.
    alerts = _feed(det, "d1", "egress_calls", [1.0, 1.0, 9999.0])
    assert alerts == []


def test_under_consumption_does_not_alert():
    det = _det()
    # Baseline ~100, then a DROP to near zero — not a resource risk.
    alerts = _feed(det, "d1", "sandbox_calls",
                   [100.0, 110.0, 90.0, 100.0, 105.0, 1.0])
    assert alerts == []


def test_sustained_spike_escalates_to_critical():
    det = _det(warn_sigma=2.0, critical_sigma=50.0, persistence_periods=2)
    # First spike → WARN (z below the very high critical sigma); a second
    # consecutive elevated period escalates to CRITICAL via persistence.
    series = [1.0, 1.0, 1.0, 1.0, 1.0, 4.0, 4.0]
    last = _feed(det, "d1", "browser_calls", series)
    sev = [a.severity for a in last]
    assert ResourceSeverity.CRITICAL in sev
    a = [a for a in last if a.severity is ResourceSeverity.CRITICAL][0]
    assert a.persistence_periods >= 2


def test_large_single_spike_is_immediately_critical():
    det = _det(warn_sigma=2.0, critical_sigma=3.0)
    # A flat baseline then a huge jump → z is effectively infinite → CRITICAL
    # on the first period (no need to wait for persistence).
    alerts = _feed(det, "d1", "llm_spend_usdc",
                   [2.0, 2.0, 2.0, 2.0, 2.0, 500.0])
    assert alerts and alerts[0].severity is ResourceSeverity.CRITICAL


def test_return_to_baseline_resets_persistence():
    det = _det()
    s = ResourceSample
    for i, v in enumerate([1.0, 1.0, 1.0, 1.0, 1.0]):
        det.observe(s(darwin_id="d1", period_iso=f"p{i}", egress_calls=v))
    det.observe(s(darwin_id="d1", period_iso="p5", egress_calls=9.0))  # spike
    tr = det.state_for("d1").dimensions["egress_calls"]
    assert tr.out_of_band_periods == 1
    det.observe(s(darwin_id="d1", period_iso="p6", egress_calls=1.0))  # back to baseline
    assert tr.out_of_band_periods == 0


def test_dimensions_are_independent():
    det = _det()
    s = ResourceSample
    # A spike in egress must not flag the (flat) sandbox dimension.
    for i in range(5):
        det.observe(s(darwin_id="d1", period_iso=f"p{i}",
                      egress_calls=1.0, sandbox_calls=5.0))
    alerts = det.observe(
        s(darwin_id="d1", period_iso="p5", egress_calls=50.0, sandbox_calls=5.0)
    )
    dims = {a.dimension for a in alerts}
    assert "egress_calls" in dims
    assert "sandbox_calls" not in dims


def test_repeat_feed_of_same_period_is_idempotent():
    det = _det()
    s = ResourceSample
    for i in range(5):
        det.observe(s(darwin_id="d1", period_iso=f"p{i}", llm_spend_usdc=1.0))
    st = det.state_for("d1")
    before = st.periods_observed
    det.observe(s(darwin_id="d1", period_iso="p4", llm_spend_usdc=99.0))  # same period
    assert st.periods_observed == before


def test_alerts_query_and_reset():
    det = _det()
    _feed(det, "d1", "llm_spend_usdc", [1.0, 1.0, 1.0, 1.0, 1.0, 50.0])
    _feed(det, "d2", "llm_spend_usdc", [1.0, 1.0, 1.0, 1.0, 1.0, 50.0])
    assert det.alerts(darwin_id="d1")
    assert det.alerts(min_severity=ResourceSeverity.CRITICAL)
    det.reset(darwin_id="d1")
    assert det.alerts(darwin_id="d1") == []
    assert det.state_for("d1") is None
    assert det.alerts(darwin_id="d2")  # untouched


def test_alert_to_json_roundtrips_fields():
    det = _det()
    alerts = _feed(det, "d1", "llm_spend_usdc", [1.0, 1.0, 1.0, 1.0, 1.0, 50.0])
    j = alerts[0].to_json()
    assert j["darwin_id"] == "d1"
    assert j["dimension"] == "llm_spend_usdc"
    assert j["severity"] in ("warn", "critical")
    assert "z_score" in j and "period_value" in j
