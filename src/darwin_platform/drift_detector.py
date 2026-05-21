"""Behaviour drift anomaly detector (docs/engineering/22 §"Anomaly detection").

The threshold-based monitor (:mod:`darwin_platform.monitoring`) catches
spending velocity, failure rate, heartbeat gaps. It misses scenarios
where a Darwin's *shape* of behaviour changes: it suddenly stops doing
what it was doing and starts doing something entirely different —
possibly a soma mutation that promoted on the harness but is misbehaving
in the wild. This module detects that.

Approach:

1. Maintain a 7-day rolling profile of each Darwin's behaviour:
   * neuron dispatch distribution (which neurons, how often)
   * spend category distribution (where the USDC goes)
   * escalation rate per category
2. Each day's profile is summarised into a vector.
3. Per-Darwin running mean + variance across the rolling window.
4. If today's vector deviates by > 2σ on any dimension → INFO alert.
5. If the deviation persists for ``persistence_days`` (default 3)
   consecutive days → WARN alert, owner notified.

The detector is read-only: it does not pause Darwins or throttle
resources. It produces :class:`DriftAlert` records the supervisor
hands to its existing alert sink. Auto-pause on drift is intentionally
out of scope — drift is *informational* (the owner reviews their
organism), not enforcement.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable


# Z-score thresholds. INFO fires on a single day's deviation; WARN
# fires when the same dimension stays out-of-band for N days.
_DEFAULT_INFO_SIGMA = 2.0
_DEFAULT_PERSISTENCE_DAYS = 3
_DEFAULT_ROLLING_WINDOW_DAYS = 7
_DEFAULT_MIN_OBSERVATIONS = 4  # below this many days, the profile is too
# noisy to flag drift on — we record but never alert.


class DriftSeverity(str, Enum):
    INFO = "info"
    WARN = "warn"


@dataclass(frozen=True)
class DriftAlert:
    """One drift alert. ``darwin_id`` is the only required field; the
    rest carries enough forensic detail that an operator can review
    the cause without re-running the detector."""

    darwin_id: str
    severity: DriftSeverity
    dimension: str            # which feature was out-of-band
    today_value: float
    rolling_mean: float
    rolling_stdev: float
    z_score: float
    persistence_days: int     # how many consecutive days out-of-band
    detected_at: float
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "darwin_id": self.darwin_id,
            "severity": self.severity.value,
            "dimension": self.dimension,
            "today_value": self.today_value,
            "rolling_mean": self.rolling_mean,
            "rolling_stdev": self.rolling_stdev,
            "z_score": self.z_score,
            "persistence_days": self.persistence_days,
            "detected_at": self.detected_at,
            "note": self.note,
        }


@dataclass(frozen=True)
class DailyProfile:
    """One day's summary of a Darwin's behaviour. The detector flattens
    this into a feature vector via :func:`_flatten`."""

    darwin_id: str
    day_iso: str               # "YYYY-MM-DD" (UTC)
    neuron_dispatches: dict[str, int]
    spend_by_category: dict[str, float]
    escalations_by_class: dict[str, int]
    total_dispatches: int
    total_spend_usdc: float

    @classmethod
    def from_audit_rows(
        cls, *, darwin_id: str, day_iso: str, rows: Iterable[dict[str, Any]],
    ) -> "DailyProfile":
        """Build a profile from a day's audit-log slice. Filters in this
        order: kind starts with 'intent:neuron.' → dispatches;
        kind contains 'payment.policy_decision' allow → spend;
        kind starts with 'event:escalation_filed' → escalations."""
        dispatches: dict[str, int] = {}
        spend: dict[str, float] = {}
        escalations: dict[str, int] = {}
        total_d = 0
        total_s = 0.0
        for r in rows:
            kind = str(r.get("kind", ""))
            payload = r.get("payload", {}) or {}
            if kind.startswith("intent:neuron."):
                # e.g. "intent:neuron.content.publisher" -> content.publisher
                neuron = kind[len("intent:neuron."):]
                dispatches[neuron] = dispatches.get(neuron, 0) + 1
                total_d += 1
            elif kind == "event:payment.policy_decision":
                decision = (payload.get("decision", {}) or {})
                if decision.get("verdict") == "allow":
                    category = str(payload.get("category", "uncategorized"))
                    amount = float(payload.get("amount_usdc", 0.0))
                    spend[category] = spend.get(category, 0.0) + amount
                    total_s += amount
            elif kind == "event:escalation_filed":
                cls_name = str(
                    (payload.get("blocker", {}) or {}).get("kind", "info")
                )
                escalations[cls_name] = escalations.get(cls_name, 0) + 1
        return cls(
            darwin_id=darwin_id, day_iso=day_iso,
            neuron_dispatches=dispatches,
            spend_by_category=spend,
            escalations_by_class=escalations,
            total_dispatches=total_d,
            total_spend_usdc=total_s,
        )


def _flatten(profile: DailyProfile) -> dict[str, float]:
    """Reduce a profile to a flat dict of named features. The detector
    runs per-dimension drift on each entry. Sparse keys (e.g. a neuron
    not dispatched today) contribute 0 to today's feature; the rolling
    window naturally handles the zero in mean/stdev."""
    out: dict[str, float] = {
        "_total_dispatches": float(profile.total_dispatches),
        "_total_spend_usdc": float(profile.total_spend_usdc),
    }
    for n, c in profile.neuron_dispatches.items():
        out[f"neuron::{n}"] = float(c)
    for cat, amt in profile.spend_by_category.items():
        out[f"spend::{cat}"] = float(amt)
    for cls_name, c in profile.escalations_by_class.items():
        out[f"escalations::{cls_name}"] = float(c)
    return out


@dataclass
class _Tracker:
    """Per-Darwin per-dimension state: rolling values + persistence
    counter."""

    values: deque[float] = field(default_factory=lambda: deque(maxlen=_DEFAULT_ROLLING_WINDOW_DAYS))
    out_of_band_days: int = 0


@dataclass
class DriftRollingState:
    """Per-Darwin running state across all dimensions."""

    darwin_id: str
    dimensions: dict[str, _Tracker] = field(default_factory=dict)
    days_observed: int = 0
    last_day_iso: str | None = None

    def update_with(self, profile: DailyProfile) -> None:
        """Append today's feature values. The detector then reads
        them and decides on alerts."""
        if profile.day_iso == self.last_day_iso:
            return  # idempotent on repeat-feed of the same day
        flat = _flatten(profile)
        keys = set(flat) | set(self.dimensions)
        for k in keys:
            tr = self.dimensions.setdefault(k, _Tracker())
            tr.values.append(flat.get(k, 0.0))
        self.days_observed += 1
        self.last_day_iso = profile.day_iso


class DriftDetector:
    """Per-fleet behaviour-drift detector. The supervisor calls
    :meth:`observe_day` once per Darwin per day with that day's
    profile and consumes the returned :class:`DriftAlert` list."""

    def __init__(
        self,
        *,
        rolling_window_days: int = _DEFAULT_ROLLING_WINDOW_DAYS,
        info_sigma: float = _DEFAULT_INFO_SIGMA,
        persistence_days: int = _DEFAULT_PERSISTENCE_DAYS,
        min_observations: int = _DEFAULT_MIN_OBSERVATIONS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._window = max(2, int(rolling_window_days))
        self._info_sigma = float(info_sigma)
        self._persistence_days = max(1, int(persistence_days))
        self._min_observations = max(2, int(min_observations))
        self._clock = clock
        self._states: dict[str, DriftRollingState] = {}
        self._alerts: list[DriftAlert] = []

    # -- write side --------------------------------------------------------
    def observe_day(
        self,
        profile: DailyProfile,
    ) -> list[DriftAlert]:
        """Update the rolling profile and return any alerts for the day."""
        state = self._states.setdefault(
            profile.darwin_id,
            DriftRollingState(darwin_id=profile.darwin_id),
        )
        # Resize deques if construction window doesn't match.
        for tr in state.dimensions.values():
            if tr.values.maxlen != self._window:
                tr.values = deque(tr.values, maxlen=self._window)
        state.update_with(profile)
        if state.days_observed < self._min_observations:
            return []

        flat = _flatten(profile)
        alerts: list[DriftAlert] = []
        for dim, tr in state.dimensions.items():
            # Compute mean/stdev over the rolling window EXCLUDING today
            # so today's value is compared against the historical mean.
            history = list(tr.values)[:-1]
            if len(history) < self._min_observations - 1:
                continue
            mean = sum(history) / len(history)
            var = sum((x - mean) ** 2 for x in history) / max(1, (len(history) - 1))
            stdev = math.sqrt(var)
            today = flat.get(dim, 0.0)
            if stdev == 0.0:
                # No historical variation: today's value is "drift" only
                # if it differs from the constant mean.
                if abs(today - mean) <= 1e-9:
                    tr.out_of_band_days = 0
                    continue
                z = float("inf")
            else:
                z = (today - mean) / stdev
            if abs(z) < self._info_sigma:
                tr.out_of_band_days = 0
                continue
            tr.out_of_band_days += 1
            severity = (
                DriftSeverity.WARN
                if tr.out_of_band_days >= self._persistence_days
                else DriftSeverity.INFO
            )
            alert = DriftAlert(
                darwin_id=profile.darwin_id,
                severity=severity,
                dimension=dim,
                today_value=today,
                rolling_mean=mean,
                rolling_stdev=stdev,
                z_score=z,
                persistence_days=tr.out_of_band_days,
                detected_at=self._clock(),
                note=(
                    f"|z|={abs(z):.2f}σ on day "
                    f"{profile.day_iso}; persisted "
                    f"{tr.out_of_band_days}/{self._persistence_days}d"
                ),
            )
            self._alerts.append(alert)
            alerts.append(alert)
        return alerts

    # -- read side ---------------------------------------------------------
    def state_for(self, darwin_id: str) -> DriftRollingState | None:
        return self._states.get(darwin_id)

    def alerts(
        self, *, darwin_id: str | None = None,
        min_severity: DriftSeverity | None = None,
    ) -> list[DriftAlert]:
        rows = self._alerts
        if darwin_id is not None:
            rows = [a for a in rows if a.darwin_id == darwin_id]
        if min_severity is not None:
            ordered = (DriftSeverity.INFO, DriftSeverity.WARN)
            min_index = ordered.index(min_severity)
            rows = [a for a in rows if ordered.index(a.severity) >= min_index]
        return list(rows)

    def reset(self, *, darwin_id: str | None = None) -> None:
        """Operator-driven reset of the rolling state. Useful after a
        confirmed soma mutation or a quarantine clearance — the
        baseline starts fresh."""
        if darwin_id is None:
            self._states.clear()
            self._alerts.clear()
            return
        self._states.pop(darwin_id, None)
        self._alerts = [a for a in self._alerts if a.darwin_id != darwin_id]
