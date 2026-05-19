"""The pause switch (docs/engineering/09 §"five-level graded response",
§"owner's available controls → Lifecycle").

Pause is **not** kill. Kill (``kill_switch.py``) is terminal: the organism
dies and there is no resurrection. Pause is the owner taking a coffee break —
"halt at the next safe checkpoint and wait for me; I'll be back". The same
life resumes, from the same checkpoint, with the same wallet and the same
unbroken audit chain. Most Darwin issues are handled at this graded level and
never reach kill.

Like the kill switch, the flag is read through a source handle that lives in
DNA and is never handed to soma code: the soma can propose patches all day, it
can never patch out a poll it cannot reach. Unlike the kill switch this poll
*does not raise* — pausing is an ordinary, reversible control-flow decision
the loop makes at a checkpoint boundary, not an exceptional unwind.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class PauseSource(Protocol):
    """The platform's authoritative pause flag (owner "Pause/Resume", or the
    supervisor's level-3 graded response). Injected at boot; read-only from
    the organism's side. Resumable: it can go true *and back to false*."""

    def is_paused(self) -> bool: ...

    def reason(self) -> str | None: ...


class _NeverPaused:
    """Default when the platform wires no pause source. A Darwin that cannot
    be paused still boots and runs (P9: never fail to boot over an optional
    control)."""

    def is_paused(self) -> bool:
        return False

    def reason(self) -> str | None:
        return None


class PauseSwitch:
    """DNA-owned poller. Construct once at boot with the platform source; keep
    the only reference in genome code so the soma cannot silence it."""

    def __init__(self, source: PauseSource | None):
        src = source if source is not None else _NeverPaused()
        if not isinstance(src, PauseSource):
            raise TypeError("pause switch needs a PauseSource")
        self._source = src

    def paused(self) -> bool:
        """Poll once. ``True`` means halt at the next checkpoint and wait —
        the loop owns the halt/resume; this never raises and never latches."""
        return bool(self._source.is_paused())

    def reason(self) -> str | None:
        return self._source.reason()
