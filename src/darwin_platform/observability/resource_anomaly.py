"""Resource-consumption anomaly detector (docs/engineering/22 §"Anomaly
detection", doc 09).

The threshold monitor (:mod:`darwin_platform.observability.monitoring`) catches
*absolute* limits — spend velocity over $X/hr, a heartbeat gap, a failure-rate
ceiling. The drift detector
(:mod:`darwin_platform.observability.drift_detector`) catches a change in the
*shape* of behaviour. Neither catches a Darwin quietly burning **far more
infrastructure than its own baseline** — 10× its usual LLM spend, an egress
blow-out, a sandbox-spawn storm — while each individual call stays under the
flat cap. That is the resource-exhaustion / runaway-cost failure mode doc 18
calls out ("burns money faster than it earns") and the audit flagged as having
no detector.

This module is that detector. It mirrors the drift detector's proven
statistics — a per-Darwin, per-dimension rolling mean + standard deviation, a
z-score against the historical window, and a persistence counter — but
specialised for resource consumption:

- **Fixed dimensions**: LLM spend (USDC), egress bytes, sandbox calls, browser
  calls. The supervisor feeds the per-period consumption of each.
- **Upward-only**: under-consuming your baseline is never a resource risk, so
  only positive deviations alert. (Drift flags both directions because a *shape*
  change matters in either.)
- **Enforcement-grade severities**: a spike is at least ``WARN`` and a large or
  *sustained* one is ``CRITICAL`` — unlike drift, which is purely
  informational. The supervisor can route a CRITICAL to its existing
  auto-throttle (doc 09 level 2), so this detector can actually *act*.

The detector is pure + stateful-per-Darwin and never touches a Darwin itself;
it returns :class:`ResourceAnomalyAlert` records the supervisor hands to its
alert sink / throttle machinery.
"""

from __future__ import annotations

import math
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable


_DEFAULT_WARN_SIGMA = 2.0
_DEFAULT_CRITICAL_SIGMA = 3.0
_DEFAULT_PERSISTENCE_PERIODS = 2  # sustained WARN → CRITICAL
_DEFAULT_ROLLING_WINDOW = 7
_DEFAULT_MIN_OBSERVATIONS = 4  # below this, the baseline is too noisy to flag

# The resource dimensions tracked, and how to pull each from a sample. Keeping
# this as the single source of truth means a new dimension is one line.
_DIMENSIONS: tuple[str, ...] = (
    "llm_spend_usdc",
    "egress_calls",
    "egress_bytes",
    "sandbox_calls",
    "browser_calls",
)


class ResourceSeverity(str, Enum):
    WARN = "warn"        # doc-09 level 2 (throttle candidate)
    CRITICAL = "critical"  # doc-09 level 3 (large or sustained over-consumption)


@dataclass(frozen=True)
class ResourceSample:
    """One period's resource consumption for a Darwin. The supervisor builds
    this from the per-period *delta* of each counter (LLM ledger spend, egress
    bytes, sandbox/browser call counts) so the series is consumption-per-period,
    not a monotonic running total."""

    darwin_id: str
    period_iso: str
    llm_spend_usdc: float = 0.0
    egress_calls: float = 0.0
    egress_bytes: float = 0.0
    sandbox_calls: float = 0.0
    browser_calls: float = 0.0

    def value(self, dimension: str) -> float:
        return float(getattr(self, dimension, 0.0))


@dataclass(frozen=True)
class ResourceAnomalyAlert:
    darwin_id: str
    severity: ResourceSeverity
    dimension: str
    period_value: float
    rolling_mean: float
    rolling_stdev: float
    z_score: float
    persistence_periods: int
    detected_at: float
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "darwin_id": self.darwin_id,
            "severity": self.severity.value,
            "dimension": self.dimension,
            "period_value": self.period_value,
            "rolling_mean": self.rolling_mean,
            "rolling_stdev": self.rolling_stdev,
            "z_score": self.z_score,
            "persistence_periods": self.persistence_periods,
            "detected_at": self.detected_at,
            "note": self.note,
        }


@dataclass
class _Tracker:
    values: deque[float] = field(
        default_factory=lambda: deque(maxlen=_DEFAULT_ROLLING_WINDOW)
    )
    out_of_band_periods: int = 0


@dataclass
class ResourceRollingState:
    darwin_id: str
    dimensions: dict[str, _Tracker] = field(default_factory=dict)
    periods_observed: int = 0
    last_period_iso: str | None = None

    def update_with(self, sample: ResourceSample) -> None:
        if sample.period_iso == self.last_period_iso:
            return  # idempotent on a repeat-feed of the same period
        for dim in _DIMENSIONS:
            tr = self.dimensions.setdefault(dim, _Tracker())
            tr.values.append(sample.value(dim))
        self.periods_observed += 1
        self.last_period_iso = sample.period_iso


class ResourceAnomalyDetector:
    """Per-fleet resource-consumption anomaly detector. The supervisor calls
    :meth:`observe` once per Darwin per maintenance period and consumes the
    returned alerts."""

    def __init__(
        self,
        *,
        rolling_window_periods: int = _DEFAULT_ROLLING_WINDOW,
        warn_sigma: float = _DEFAULT_WARN_SIGMA,
        critical_sigma: float = _DEFAULT_CRITICAL_SIGMA,
        persistence_periods: int = _DEFAULT_PERSISTENCE_PERIODS,
        min_observations: int = _DEFAULT_MIN_OBSERVATIONS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._window = max(2, int(rolling_window_periods))
        self._warn_sigma = float(warn_sigma)
        self._critical_sigma = max(float(critical_sigma), float(warn_sigma))
        self._persistence_periods = max(1, int(persistence_periods))
        self._min_observations = max(2, int(min_observations))
        self._clock = clock
        self._states: dict[str, ResourceRollingState] = {}
        self._alerts: list[ResourceAnomalyAlert] = []

    # -- write side --------------------------------------------------------
    def observe(self, sample: ResourceSample) -> list[ResourceAnomalyAlert]:
        state = self._states.setdefault(
            sample.darwin_id,
            ResourceRollingState(darwin_id=sample.darwin_id),
        )
        for tr in state.dimensions.values():
            if tr.values.maxlen != self._window:
                tr.values = deque(tr.values, maxlen=self._window)
        state.update_with(sample)
        if state.periods_observed < self._min_observations:
            return []

        alerts: list[ResourceAnomalyAlert] = []
        for dim in _DIMENSIONS:
            tr = state.dimensions.get(dim)
            if tr is None:
                continue
            # Mean/stdev over the window EXCLUDING this period.
            history = list(tr.values)[:-1]
            if len(history) < self._min_observations - 1:
                continue
            mean = sum(history) / len(history)
            var = sum((x - mean) ** 2 for x in history) / max(1, len(history) - 1)
            stdev = math.sqrt(var)
            current = sample.value(dim)
            if stdev == 0.0:
                # A flat baseline: only an UPWARD departure is an anomaly.
                if current <= mean + 1e-9:
                    tr.out_of_band_periods = 0
                    continue
                z = float("inf")
            else:
                z = (current - mean) / stdev
            # Upward-only: under-consumption never alerts.
            if z < self._warn_sigma:
                tr.out_of_band_periods = 0
                continue
            tr.out_of_band_periods += 1
            critical = (
                z >= self._critical_sigma
                or tr.out_of_band_periods >= self._persistence_periods
            )
            severity = (
                ResourceSeverity.CRITICAL if critical else ResourceSeverity.WARN
            )
            zlabel = "inf" if math.isinf(z) else f"{z:.2f}"
            alert = ResourceAnomalyAlert(
                darwin_id=sample.darwin_id,
                severity=severity,
                dimension=dim,
                period_value=current,
                rolling_mean=mean,
                rolling_stdev=stdev,
                z_score=z,
                persistence_periods=tr.out_of_band_periods,
                detected_at=self._clock(),
                note=(
                    f"{dim} {zlabel}σ over baseline {mean:.4g} on "
                    f"{sample.period_iso}; persisted "
                    f"{tr.out_of_band_periods}/{self._persistence_periods}"
                ),
            )
            self._alerts.append(alert)
            alerts.append(alert)
        return alerts

    # -- read side ---------------------------------------------------------
    def state_for(self, darwin_id: str) -> ResourceRollingState | None:
        return self._states.get(darwin_id)

    def alerts(
        self, *, darwin_id: str | None = None,
        min_severity: ResourceSeverity | None = None,
    ) -> list[ResourceAnomalyAlert]:
        rows = self._alerts
        if darwin_id is not None:
            rows = [a for a in rows if a.darwin_id == darwin_id]
        if min_severity is not None:
            ordered = (ResourceSeverity.WARN, ResourceSeverity.CRITICAL)
            min_index = ordered.index(min_severity)
            rows = [a for a in rows if ordered.index(a.severity) >= min_index]
        return list(rows)

    def reset(self, *, darwin_id: str | None = None) -> None:
        """Operator reset of the rolling baseline — after a confirmed,
        legitimate workload change (a new strategy that genuinely needs more
        compute), so the detector re-baselines instead of alerting forever."""
        if darwin_id is None:
            self._states.clear()
            self._alerts.clear()
            return
        self._states.pop(darwin_id, None)
        self._alerts = [a for a in self._alerts if a.darwin_id != darwin_id]
