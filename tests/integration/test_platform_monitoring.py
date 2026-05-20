"""Quotas + anomaly detection (docs/engineering/08, 09).

Detectors are pure functions of (signals, quotas) → Alert | None — these
tests synthesize signals directly. The integration tests below exercise the
full Supervisor → Monitor wiring and the optional auto-pause behaviour.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from darwin_platform import (
    DarwinSignals,
    Monitor,
    Quotas,
    Severity,
    Supervisor,
    run_detectors,
)
from darwin_platform.monitoring import (
    detect_escalation_rate,
    detect_failure_rate,
    detect_heartbeat_health,
    detect_spend_velocity,
)


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


def _signals(**overrides) -> DarwinSignals:
    defaults = dict(
        darwin_id="d1",
        now=1_700_000_000.0,
        alive=True,
        paused=False,
        state_tick=10,
        last_tick_advance_ts=1_700_000_000.0,
        movements=(),
        dispatches_done=0,
        successful_dispatches=0,
        recurring_failures={},
        ticket_opened_ts=(),
    )
    defaults.update(overrides)
    return DarwinSignals(**defaults)


# -- spend velocity --------------------------------------------------------
def test_spend_velocity_clear_under_threshold():
    s = _signals(
        movements=(
            {"debit": {"amount_usdc": 10.0, "ts": 1_700_000_000.0 - 60}},
            {"debit": {"amount_usdc": 10.0, "ts": 1_700_000_000.0 - 30}},
        )
    )
    assert detect_spend_velocity(s, Quotas()) is None


def test_spend_velocity_warn():
    s = _signals(
        movements=tuple(
            {"debit": {"amount_usdc": 25.0, "ts": 1_700_000_000.0 - 60}}
            for _ in range(5)  # $125/hr — over $100 warn, under $300 critical
        )
    )
    alert = detect_spend_velocity(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.WARN
    assert alert.detail["usd_per_hour"] == pytest.approx(125.0)


def test_spend_velocity_critical():
    s = _signals(
        movements=tuple(
            {"debit": {"amount_usdc": 80.0, "ts": 1_700_000_000.0 - 60}}
            for _ in range(5)  # $400/hr — over $300 critical
        )
    )
    alert = detect_spend_velocity(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.CRITICAL
    assert alert.suggested_level == 3


def test_spend_velocity_ignores_old_debits():
    """Debits older than one hour must not count toward the velocity."""
    s = _signals(
        movements=tuple(
            {"debit": {"amount_usdc": 1000.0, "ts": 1_700_000_000.0 - 7_200}}
            for _ in range(5)
        )
    )
    assert detect_spend_velocity(s, Quotas()) is None


# -- failure rate ----------------------------------------------------------
def test_failure_rate_below_min_dispatches_silent():
    s = _signals(dispatches_done=3, successful_dispatches=0)
    # Despite a 100% failure rate, the sample is too small to alert.
    assert detect_failure_rate(s, Quotas()) is None


def test_failure_rate_warn():
    # 20 dispatches, 8 failures = 40% > 30% warn
    s = _signals(dispatches_done=20, successful_dispatches=12)
    alert = detect_failure_rate(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.WARN


def test_failure_rate_critical():
    # 20 dispatches, 14 failures = 70% > 60% critical
    s = _signals(dispatches_done=20, successful_dispatches=6)
    alert = detect_failure_rate(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.CRITICAL


# -- heartbeat health ------------------------------------------------------
def test_heartbeat_clear_recent_tick():
    s = _signals(last_tick_advance_ts=1_700_000_000.0 - 60)  # 60s ago
    assert detect_heartbeat_health(s, Quotas()) is None


def test_heartbeat_warn_on_stale_tick():
    s = _signals(last_tick_advance_ts=1_700_000_000.0 - 1500)  # 25min ago
    alert = detect_heartbeat_health(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.WARN


def test_heartbeat_critical_on_very_stale_tick():
    s = _signals(last_tick_advance_ts=1_700_000_000.0 - 4000)  # 66min ago
    alert = detect_heartbeat_health(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.CRITICAL


def test_heartbeat_suppressed_for_paused():
    """A paused Darwin doesn't tick by design — that's level-3 graded
    response, not a heartbeat failure. Suppress the false positive."""
    s = _signals(paused=True, last_tick_advance_ts=1_700_000_000.0 - 10_000)
    assert detect_heartbeat_health(s, Quotas()) is None


def test_heartbeat_suppressed_for_dead():
    s = _signals(alive=False, last_tick_advance_ts=1_700_000_000.0 - 10_000)
    assert detect_heartbeat_health(s, Quotas()) is None


# -- escalation rate -------------------------------------------------------
def test_escalation_rate_clear_few_recent():
    s = _signals(ticket_opened_ts=tuple([1_700_000_000.0 - 60] * 3))
    assert detect_escalation_rate(s, Quotas()) is None


def test_escalation_rate_warn():
    s = _signals(ticket_opened_ts=tuple([1_700_000_000.0 - 60] * 6))
    alert = detect_escalation_rate(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.WARN


def test_escalation_rate_critical():
    s = _signals(ticket_opened_ts=tuple([1_700_000_000.0 - 60] * 25))
    alert = detect_escalation_rate(s, Quotas())
    assert alert is not None
    assert alert.severity is Severity.CRITICAL


# -- run_detectors composes them ------------------------------------------
def test_run_detectors_returns_all_fired():
    s = _signals(
        dispatches_done=20,
        successful_dispatches=6,  # critical failure rate
        ticket_opened_ts=tuple([1_700_000_000.0 - 60] * 25),  # critical escalations
    )
    alerts = run_detectors(s, Quotas())
    kinds = {a.kind for a in alerts}
    assert "failure_rate" in kinds
    assert "escalation_rate" in kinds


# -- Monitor — dedup, tick freshness, filtering ---------------------------
def test_monitor_dedups_repeated_alerts():
    now = [1_700_000_000.0]
    m = Monitor(quotas=Quotas(dedup_window_seconds=60.0), clock=lambda: now[0])
    s = _signals(dispatches_done=20, successful_dispatches=6, now=now[0])
    a1 = m.check(s)
    assert len(a1) == 1

    # Same signals at the same clock — deduped.
    a2 = m.check(s)
    assert a2 == []

    # Past the dedup window — fires again.
    now[0] += 120.0
    a3 = m.check(_signals(dispatches_done=20, successful_dispatches=6, now=now[0]))
    assert len(a3) == 1


def test_monitor_observe_tick_freshness():
    now = [1_000.0]
    m = Monitor(clock=lambda: now[0])
    first = m.observe_tick("d1", tick=5)
    assert first == 1_000.0
    now[0] = 1_005.0
    # Same tick — does NOT refresh.
    assert m.observe_tick("d1", tick=5) == 1_000.0
    now[0] = 1_010.0
    # New tick — refreshes.
    assert m.observe_tick("d1", tick=6) == 1_010.0


def test_monitor_filters_by_severity_and_since():
    now = [0.0]
    m = Monitor(clock=lambda: now[0])
    m.check(_signals(dispatches_done=20, successful_dispatches=12, now=now[0]))  # warn
    now[0] += 1000.0
    m.check(_signals(dispatches_done=20, successful_dispatches=6, now=now[0]))  # critical

    all_alerts = m.alerts_for("d1")
    assert len(all_alerts) == 2

    crit_only = m.alerts_for("d1", min_severity=Severity.CRITICAL)
    assert len(crit_only) == 1
    assert crit_only[0].severity is Severity.CRITICAL

    recent = m.alerts_for("d1", since=500.0)
    assert len(recent) == 1
    assert recent[0].severity is Severity.CRITICAL


def test_monitor_detector_bug_does_not_crash():
    class BrokenSink:
        def emit(self, alert):
            raise RuntimeError("page failed")

    m = Monitor(sink=BrokenSink())
    # The detector finds something; the sink raises; the monitor still
    # records the alert and returns it (no exception propagated).
    out = m.check(_signals(dispatches_done=20, successful_dispatches=6))
    assert len(out) == 1


# -- Integration: Supervisor + Monitor ------------------------------------
async def test_supervisor_emits_alerts_through_monitor() -> None:
    """A live Darwin with a tight failure-rate quota: the monitor sees the
    genome's dispatches and trips the warn threshold within a few cycles."""
    quotas = Quotas(
        failure_rate_warn=0.01,        # any failure is a warn
        failure_rate_critical=0.99,    # but not critical
        failure_rate_min_dispatches=1, # don't wait for a big sample
        # Keep other detectors quiet so this test isolates failure_rate.
        spend_velocity_usd_per_hour_warn=1_000_000.0,
        spend_velocity_usd_per_hour_critical=1_000_000.0,
        heartbeat_warn_gap_seconds=1_000_000.0,
        heartbeat_critical_gap_seconds=1_000_000.0,
        escalation_rate_per_hour_warn=1_000_000,
        escalation_rate_per_hour_critical=1_000_000,
        dedup_window_seconds=0.0,
    )
    sup = Supervisor(
        heartbeat_seconds=0.0,
        quotas=quotas,
        monitor_every_n_ticks=1,
    )
    try:
        status = await sup.spawn(opening_usdc=60.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        # Hand-craft a failed dispatch so the failure_rate detector fires
        # without waiting for the genome to fail something on its own.
        h.runtime.state.dispatches_done = 5
        h.runtime.state.successful_dispatches = 3  # 40% failure rate
        await _wait_until(
            lambda: any(a.kind == "failure_rate"
                        for a in sup.monitor.alerts_for(status.darwin_id))
        )
    finally:
        await sup.shutdown()


async def test_supervisor_auto_pause_on_critical() -> None:
    """With ``auto_pause_on_critical=True``, a CRITICAL alert pauses the
    Darwin. Without it (the safe default), the alert fires but the Darwin
    keeps ticking — surfaced for the operator to act on."""
    quotas = Quotas(
        spend_velocity_usd_per_hour_warn=1_000_000.0,
        spend_velocity_usd_per_hour_critical=1_000_000.0,
        failure_rate_warn=0.01,
        failure_rate_critical=0.50,
        failure_rate_min_dispatches=1,
        heartbeat_warn_gap_seconds=1_000_000.0,
        heartbeat_critical_gap_seconds=1_000_000.0,
        escalation_rate_per_hour_warn=1_000_000,
        escalation_rate_per_hour_critical=1_000_000,
        dedup_window_seconds=0.0,
    )

    # Default supervisor: no auto-pause.
    sup = Supervisor(
        heartbeat_seconds=0.0, quotas=quotas, monitor_every_n_ticks=1
    )
    try:
        status = await sup.spawn(opening_usdc=60.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        h.runtime.state.dispatches_done = 10
        h.runtime.state.successful_dispatches = 3  # 70% > critical
        await _wait_until(
            lambda: any(a.severity is Severity.CRITICAL
                        for a in sup.monitor.alerts_for(status.darwin_id))
        )
        # Not auto-paused.
        assert not h.runtime.state.paused
    finally:
        await sup.shutdown()

    # Opt-in: auto-pause on critical.
    sup = Supervisor(
        heartbeat_seconds=0.0,
        quotas=quotas,
        monitor_every_n_ticks=1,
        auto_pause_on_critical=True,
    )
    try:
        status = await sup.spawn(opening_usdc=60.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        h.runtime.state.dispatches_done = 10
        h.runtime.state.successful_dispatches = 3
        await _wait_until(lambda: h.runtime.state.paused)
        # ``auto_paused_for`` is latched to the alert kind so the same
        # kind doesn't re-pause an already-paused Darwin.
        assert h.auto_paused_for == "failure_rate"
    finally:
        await sup.shutdown()


async def test_alerts_api_endpoint() -> None:
    """Smoke test for the FastAPI /alerts surface."""
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform.api import build_app  # type: ignore

    quotas = Quotas(
        failure_rate_warn=0.01,
        failure_rate_critical=0.99,
        failure_rate_min_dispatches=1,
        spend_velocity_usd_per_hour_warn=1_000_000.0,
        spend_velocity_usd_per_hour_critical=1_000_000.0,
        heartbeat_warn_gap_seconds=1_000_000.0,
        heartbeat_critical_gap_seconds=1_000_000.0,
        escalation_rate_per_hour_warn=1_000_000,
        escalation_rate_per_hour_critical=1_000_000,
        dedup_window_seconds=0.0,
    )
    sup = Supervisor(heartbeat_seconds=0.0, quotas=quotas, monitor_every_n_ticks=1)
    app = build_app(sup)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post("/darwins", json={"opening_usdc": 60.0})
            did = r.json()["darwin_id"]
            h = sup._darwins[did]  # noqa: SLF001
            h.runtime.state.dispatches_done = 5
            h.runtime.state.successful_dispatches = 3
            await _wait_until(
                lambda: any(a.kind == "failure_rate"
                            for a in sup.monitor.alerts_for(did))
            )

            r = await c.get(f"/darwins/{did}/alerts")
            body = r.json()
            assert body["darwin_id"] == did
            assert any(a["kind"] == "failure_rate" for a in body["alerts"])

            r = await c.get(
                "/platform/alerts", params={"min_severity": "warn"}
            )
            assert any(a["darwin_id"] == did for a in r.json()["alerts"])

            # Bad severity → 400.
            r = await c.get("/platform/alerts", params={"min_severity": "extreme"})
            assert r.status_code == 400
        finally:
            await sup.shutdown()
