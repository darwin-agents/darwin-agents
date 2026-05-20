"""Quotas + anomaly detection (docs/engineering/08, 09).

The supervisor watches every Darwin for resource consumption, spend velocity,
failure rate, heartbeat health, and escalation rate. Thresholds trigger
doc-09's graded responses:

  1. Log + dashboard warning  → INFO
  2. Throttle resource         → WARN (throttle wiring is deferred; we alert)
  3. Pause + notify owner      → CRITICAL (optionally auto-paused)
  4. Quarantine                → out of MVP (state machine + review process)
  5. Force-kill                → never auto — doc 09: "Level 5 always requires
                                 a human operator approval — never auto-fired"

Design notes:

- **Detectors are pure functions** of (signals, quotas) → Alert | None. That
  makes them trivially unit-testable with synthesized signals, and decouples
  them from the Supervisor's internals (which the tests don't need to know).
- **Cooldown per (darwin_id, kind, severity)** avoids spamming the sink on a
  sustained breach. Default 60s; tunable per Monitor instance.
- **Auto-pause is opt-in.** A misconfigured threshold could pause every
  Darwin in the fleet; the default Supervisor wiring only logs alerts.
- **No auto-kill**, ever — per doc 09. The Monitor surfaces CRITICAL alerts;
  a human operator decides whether to escalate to kill.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, runtime_checkable


# -- severity / quotas ----------------------------------------------------
class Severity(str, Enum):
    INFO = "info"  # doc-09 level 1
    WARN = "warn"  # doc-09 level 2 (throttle — deferred)
    CRITICAL = "critical"  # doc-09 level 3 (pause + notify)


_LEVEL_FOR_SEVERITY: dict[Severity, int] = {
    Severity.INFO: 1,
    Severity.WARN: 2,
    Severity.CRITICAL: 3,
}


@dataclass(frozen=True)
class Quotas:
    """Per-Darwin thresholds for the four detectors. Defaults are deliberately
    loose — a normal default-scenario Darwin should not trip any of them.
    Operators tighten per-fleet; tests construct tighter quotas to force the
    breach."""

    # Spend velocity: USD debited in the past hour. The genome's per-day hard
    # cap is $500 (HARD_CAPS) so >>$100/hour is suspicious; >>$300/hour is
    # almost certainly the wallet under attack.
    spend_velocity_usd_per_hour_warn: float = 100.0
    spend_velocity_usd_per_hour_critical: float = 300.0

    # Failure rate over the lifetime of the Darwin (no rolling window for the
    # MVP — recurring_failures already tracks per-class for the genome's own
    # mutation gates; this is the gross signal).
    failure_rate_warn: float = 0.30  # 30% of dispatches failing
    failure_rate_critical: float = 0.60
    failure_rate_min_dispatches: int = 10  # don't alert on tiny samples

    # Heartbeat health: time since we last saw the tick advance. The genome's
    # heartbeat cap is 600s; >2× that is a stuck loop, >5× is a dead organism
    # the supervisor missed.
    heartbeat_warn_gap_seconds: float = 1_200.0  # 2× heartbeat cap
    heartbeat_critical_gap_seconds: float = 3_000.0  # 5×

    # Escalation rate: tickets opened in the past hour. >5/hr suggests the
    # Darwin is stuck asking; >20/hr suggests prompt-injection or a UI loop.
    escalation_rate_per_hour_warn: int = 5
    escalation_rate_per_hour_critical: int = 20

    # Dedup window per (darwin_id, kind, severity). Lower for tests, raise
    # for prod so a sustained breach doesn't fan out hourly.
    dedup_window_seconds: float = 60.0


# -- alerts ---------------------------------------------------------------
@dataclass(frozen=True)
class Alert:
    """One detector finding. Frozen so an emitted alert can never be rewritten
    after a sink has acted on it."""

    darwin_id: str
    kind: str  # "spend_velocity" | "failure_rate" | "heartbeat" | "escalation_rate"
    severity: Severity
    suggested_level: int  # doc-09 graded-response level (1-5)
    detail: dict[str, Any]
    ts: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "darwin_id": self.darwin_id,
            "kind": self.kind,
            "severity": self.severity.value,
            "suggested_level": self.suggested_level,
            "detail": dict(self.detail),
            "ts": self.ts,
        }


def _alert(
    darwin_id: str, kind: str, severity: Severity, detail: dict[str, Any], now: float
) -> Alert:
    return Alert(
        darwin_id=darwin_id,
        kind=kind,
        severity=severity,
        suggested_level=_LEVEL_FOR_SEVERITY[severity],
        detail=detail,
        ts=now,
    )


@runtime_checkable
class AlertSink(Protocol):
    """Where alerts go. Real impls page on-call, write to a SIEM, etc."""

    def emit(self, alert: Alert) -> None: ...


class LogAlertSink:
    """Default in-memory sink. Records every emit() so tests + ops can read
    what would have been sent. Never raises."""

    def __init__(self) -> None:
        self.alerts: list[Alert] = []

    def emit(self, alert: Alert) -> None:
        self.alerts.append(alert)


# -- signals --------------------------------------------------------------
@dataclass(frozen=True)
class DarwinSignals:
    """Read-only snapshot of one Darwin at one moment. Detectors look at this
    only — they do not reach into the Supervisor or the runtime."""

    darwin_id: str
    now: float
    alive: bool
    paused: bool
    state_tick: int
    last_tick_advance_ts: float
    movements: tuple[dict[str, Any], ...]
    dispatches_done: int
    successful_dispatches: int
    recurring_failures: dict[str, int]
    ticket_opened_ts: tuple[float, ...]  # opened_ts of every ticket seen


# -- detectors ------------------------------------------------------------
def detect_spend_velocity(s: DarwinSignals, q: Quotas) -> Alert | None:
    cutoff = s.now - 3_600.0
    total = 0.0
    for mv in s.movements:
        debit = mv.get("debit")
        if debit and float(debit.get("ts", 0.0)) >= cutoff:
            total += float(debit.get("amount_usdc", 0.0))
    if total >= q.spend_velocity_usd_per_hour_critical:
        return _alert(
            s.darwin_id,
            "spend_velocity",
            Severity.CRITICAL,
            {"usd_per_hour": total, "threshold": q.spend_velocity_usd_per_hour_critical},
            s.now,
        )
    if total >= q.spend_velocity_usd_per_hour_warn:
        return _alert(
            s.darwin_id,
            "spend_velocity",
            Severity.WARN,
            {"usd_per_hour": total, "threshold": q.spend_velocity_usd_per_hour_warn},
            s.now,
        )
    return None


def detect_failure_rate(s: DarwinSignals, q: Quotas) -> Alert | None:
    if s.dispatches_done < q.failure_rate_min_dispatches:
        return None
    failures = max(0, s.dispatches_done - s.successful_dispatches)
    rate = failures / s.dispatches_done
    if rate >= q.failure_rate_critical:
        return _alert(
            s.darwin_id,
            "failure_rate",
            Severity.CRITICAL,
            {
                "rate": rate,
                "failures": failures,
                "dispatches": s.dispatches_done,
                "threshold": q.failure_rate_critical,
            },
            s.now,
        )
    if rate >= q.failure_rate_warn:
        return _alert(
            s.darwin_id,
            "failure_rate",
            Severity.WARN,
            {
                "rate": rate,
                "failures": failures,
                "dispatches": s.dispatches_done,
                "threshold": q.failure_rate_warn,
            },
            s.now,
        )
    return None


def detect_heartbeat_health(s: DarwinSignals, q: Quotas) -> Alert | None:
    # Paused Darwins don't tick — that's deliberate (doc 09 level-3). Suppress.
    if s.paused or not s.alive:
        return None
    gap = max(0.0, s.now - s.last_tick_advance_ts)
    if gap >= q.heartbeat_critical_gap_seconds:
        return _alert(
            s.darwin_id,
            "heartbeat",
            Severity.CRITICAL,
            {"gap_seconds": gap, "threshold": q.heartbeat_critical_gap_seconds},
            s.now,
        )
    if gap >= q.heartbeat_warn_gap_seconds:
        return _alert(
            s.darwin_id,
            "heartbeat",
            Severity.WARN,
            {"gap_seconds": gap, "threshold": q.heartbeat_warn_gap_seconds},
            s.now,
        )
    return None


def detect_escalation_rate(s: DarwinSignals, q: Quotas) -> Alert | None:
    cutoff = s.now - 3_600.0
    recent = sum(1 for ts in s.ticket_opened_ts if ts >= cutoff)
    if recent >= q.escalation_rate_per_hour_critical:
        return _alert(
            s.darwin_id,
            "escalation_rate",
            Severity.CRITICAL,
            {"per_hour": recent, "threshold": q.escalation_rate_per_hour_critical},
            s.now,
        )
    if recent >= q.escalation_rate_per_hour_warn:
        return _alert(
            s.darwin_id,
            "escalation_rate",
            Severity.WARN,
            {"per_hour": recent, "threshold": q.escalation_rate_per_hour_warn},
            s.now,
        )
    return None


_DETECTORS: tuple[Callable[[DarwinSignals, Quotas], Alert | None], ...] = (
    detect_spend_velocity,
    detect_failure_rate,
    detect_heartbeat_health,
    detect_escalation_rate,
)


def run_detectors(signals: DarwinSignals, quotas: Quotas) -> list[Alert]:
    """Run every detector over a signals snapshot. A detector that raises is
    a bug in the detector — the Monitor itself does the catch (so the
    Supervisor's drive loop never crashes); tests prefer this raw form so a
    detector bug shows up directly."""
    out: list[Alert] = []
    for det in _DETECTORS:
        a = det(signals, quotas)
        if a is not None:
            out.append(a)
    return out


# -- the monitor ----------------------------------------------------------
class Monitor:
    """Stateful runner around the pure detectors.

    State:
      - per-Darwin alert history
      - per-(darwin, kind, severity) last-emit timestamp for dedup
      - tick-advance bookkeeping (so heartbeat detection can pinpoint when
        the tick *stopped* moving, not just when we last looked)

    Construct one per Supervisor process. The Supervisor calls ``observe()``
    each drive cycle to refresh tick freshness, and ``check()`` to evaluate
    detectors and dispatch alerts.
    """

    def __init__(
        self,
        *,
        quotas: Quotas | None = None,
        sink: AlertSink | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.quotas = quotas or Quotas()
        self._sink: AlertSink = sink or LogAlertSink()
        self._clock = clock
        self._alerts: dict[str, list[Alert]] = {}
        self._dedup: dict[tuple[str, str, Severity], float] = {}
        self._last_tick: dict[str, tuple[int, float]] = {}

    # -- tick freshness --------------------------------------------------
    def observe_tick(self, darwin_id: str, tick: int) -> float:
        """Record the current tick. Returns the timestamp when the tick last
        ADVANCED — heartbeat detection uses this to spot a stuck loop. A
        repeated observation at the same tick does NOT refresh the timestamp;
        that's the whole point."""
        prev = self._last_tick.get(darwin_id)
        now = self._clock()
        if prev is None or prev[0] != tick:
            self._last_tick[darwin_id] = (tick, now)
            return now
        return prev[1]

    def last_tick_advance(self, darwin_id: str) -> float:
        prev = self._last_tick.get(darwin_id)
        return prev[1] if prev is not None else self._clock()

    def forget(self, darwin_id: str) -> None:
        self._last_tick.pop(darwin_id, None)

    # -- checking --------------------------------------------------------
    def check(self, signals: DarwinSignals) -> list[Alert]:
        """Run detectors, dedup, dispatch to sink, return what fired."""
        emitted: list[Alert] = []
        for det in _DETECTORS:
            try:
                alert = det(signals, self.quotas)
            except Exception:
                continue  # a detector bug must not crash the supervisor
            if alert is None:
                continue
            key = (alert.darwin_id, alert.kind, alert.severity)
            last_ts = self._dedup.get(key)
            if last_ts is not None and (alert.ts - last_ts) < self.quotas.dedup_window_seconds:
                continue
            self._dedup[key] = alert.ts
            self._alerts.setdefault(alert.darwin_id, []).append(alert)
            try:
                self._sink.emit(alert)
            except Exception:
                # A misbehaving sink must not block the supervisor.
                pass
            emitted.append(alert)
        return emitted

    # -- reads -----------------------------------------------------------
    def alerts_for(
        self,
        darwin_id: str,
        *,
        min_severity: Severity | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        rows = list(self._alerts.get(darwin_id, []))
        return _filter(rows, min_severity=min_severity, since=since, limit=limit)

    def all_alerts(
        self,
        *,
        min_severity: Severity | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[Alert]:
        rows = [a for alerts in self._alerts.values() for a in alerts]
        rows.sort(key=lambda a: a.ts)
        return _filter(rows, min_severity=min_severity, since=since, limit=limit)

    @property
    def sink(self) -> AlertSink:
        return self._sink


_SEVERITY_RANK = {Severity.INFO: 0, Severity.WARN: 1, Severity.CRITICAL: 2}


def _filter(
    rows: list[Alert],
    *,
    min_severity: Severity | None,
    since: float | None,
    limit: int | None,
) -> list[Alert]:
    if min_severity is not None:
        min_rank = _SEVERITY_RANK[min_severity]
        rows = [a for a in rows if _SEVERITY_RANK[a.severity] >= min_rank]
    if since is not None:
        rows = [a for a in rows if a.ts >= since]
    if limit is not None:
        rows = rows[-limit:]
    return rows
