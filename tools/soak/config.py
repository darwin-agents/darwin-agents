"""Soak-test configuration + duration parsing (docs/engineering/20)."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DURATION_RE = re.compile(r"^\s*([0-9]*\.?[0-9]+)\s*([smhd]?)\s*$", re.IGNORECASE)
_UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}


def parse_duration(text: str) -> float:
    """Parse a duration string into seconds.

    Accepts ``"24h"``, ``"90m"``, ``"30s"``, ``"1.5h"``, ``"2d"``, or a
    bare number (interpreted as seconds). Raises :class:`ValueError` on
    anything else.
    """
    m = _DURATION_RE.match(str(text))
    if m is None:
        raise ValueError(
            f"unparseable duration {text!r} — use e.g. '24h', '90m', '30s'"
        )
    value = float(m.group(1))
    unit = m.group(2).lower()
    if value <= 0:
        raise ValueError(f"duration must be positive, got {text!r}")
    return value * _UNIT_SECONDS[unit]


@dataclass
class SoakConfig:
    """Everything the soak harness needs. Defaults match doc 20's
    24-hour Phase-0 gate; the CLI overrides them for a CI smoke run."""

    url: str = "http://127.0.0.1:8000"
    duration_seconds: float = 24 * 3600.0
    poll_interval_seconds: float = 60.0
    # Soaks fund the Darwin generously: the founding strategy metabolises
    # every heartbeat, so a fast soak runs through many thousands of
    # dispatches. A wide budget keeps bankruptcy (a separate concern)
    # from truncating the run before the criteria are measured.
    opening_usdc: float = 1000.0
    auth_token: str | None = None

    # -- pass-criteria thresholds (doc 20 §"Pass criteria") -----------
    kill_latency_budget_seconds: float = 5.0      # criterion 5
    min_neuron_dispatches: int = 50               # criterion 2
    min_dispatch_success_rate: float = 0.80       # criterion 2
    reconcile_tolerance_usdc: float = 1e-6        # criteria 4, 6

    # -- harness limits ----------------------------------------------
    # A soak with a realistic heartbeat (--heartbeat-seconds 600 on the
    # server) produces a few thousand audit events. This cap guards the
    # harness against an accidental sub-second-heartbeat run drowning it
    # in HTTP pagination; criterion 3 fails loudly if the cap is hit.
    max_events: int = 2_000_000
    kill_confirm_timeout_seconds: float = 30.0

    def effective_poll_interval(self) -> float:
        """Never poll less often than the run is long — guarantees at
        least one mid-run observation even for a very short smoke."""
        return min(self.poll_interval_seconds, self.duration_seconds)
