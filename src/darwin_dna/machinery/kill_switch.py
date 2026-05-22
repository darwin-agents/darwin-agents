"""The off switch (docs/engineering/18 hot path #2, P8: kill-switches are sacred).

Polls a platform-controlled flag at every main-loop iteration and on entry to
every neuron dispatch. When flipped, the Darwin halts *before* its next
side-effecting action.

The single most important property: **the soma cannot silence it.** The flag
is read through a source handle that lives in DNA and is never handed to soma
code. The soma can propose patches all day; it can never patch out a poll it
cannot reach.

The second property: **it fails closed.** If the platform source cannot be
reached at all, the Darwin is treated as killed. An off switch that fails open
is not an off switch (docs/engineering/24 & 28: "source unreachable -> treat
as killed").
"""

from __future__ import annotations

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class KillSwitchSource(Protocol):
    """The platform's authoritative kill flag. Injected at boot by the
    platform (the real one polls a control-plane endpoint; the fake reads an
    in-memory flag). Read-only from the organism's side."""

    def is_killed(self) -> bool: ...

    def reason(self) -> str | None: ...


class KillSignal(BaseException):
    """Raised when the switch is flipped. A ``BaseException``, not an
    ``Exception``, so a careless ``except Exception`` in soma-evolved code
    cannot swallow it. The loop catches this specifically and runs the halt
    sequence; nothing else may."""

    def __init__(self, reason: str | None, observed_at: float):
        self.reason = reason or "platform kill switch flipped"
        self.observed_at = observed_at
        super().__init__(f"KILLED: {self.reason}")


class KillSwitch:
    """DNA-owned poller. Construct once at boot with the platform source;
    keep the only reference in genome code."""

    def __init__(self, source: KillSwitchSource):
        if not isinstance(source, KillSwitchSource):
            raise TypeError("kill switch needs a KillSwitchSource")
        self._source = source
        self._tripped_at: float | None = None
        self._tripped_reason: str | None = None

    def tripped(self) -> bool:
        return self._tripped_at is not None

    def check(self) -> None:
        """Poll once. Raise :class:`KillSignal` if killed. Call this at the
        top of every heartbeat and immediately before every side-effecting
        action. Idempotent once tripped (stays tripped — a kill is final).

        Fails closed: if polling the platform source raises at all (an
        unreachable control plane, a timeout), the Darwin is tripped as if
        killed. We would rather wrongly halt a healthy Darwin than let one
        keep spending money while its off switch is unreachable."""
        if self._tripped_at is not None:
            raise KillSignal(self._tripped_reason, self._tripped_at)
        try:
            killed = self._source.is_killed()
            reason = self._source.reason() if killed else None
        except Exception as exc:  # noqa: BLE001 - fail closed on ANY source error
            self._tripped_at = time.time()
            self._tripped_reason = (
                f"kill switch source unreachable ({type(exc).__name__}: {exc})"
            )
            raise KillSignal(self._tripped_reason, self._tripped_at) from exc
        if killed:
            self._tripped_at = time.time()
            self._tripped_reason = reason
            raise KillSignal(self._tripped_reason, self._tripped_at)
