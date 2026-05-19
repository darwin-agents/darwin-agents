"""The mutation archive (docs/engineering/06).

Beyond direct parent→child inheritance, a lineage keeps a bounded memory of
*negative* lessons: mutation attempts that were proposed, promoted, or
rejected, so the lineage does not re-try the same dead end. Bounded on purpose
(``archive_max_entries``) — an unbounded archive is just a slow memory leak
with extra steps.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Literal

from pydantic import BaseModel

Outcome = Literal["proposed", "promoted", "rejected"]


class ArchiveEntry(BaseModel):
    model_config = {"frozen": True}

    ts: float
    failure_class: str
    outcome: Outcome
    rationale: str


class MutationArchive:
    """A ring buffer of mutation outcomes. Append-only from the caller's
    side; the oldest entries fall off the end at the cap."""

    def __init__(self, max_entries: int):
        self._dq: deque[ArchiveEntry] = deque(maxlen=max_entries)

    def record(self, failure_class: str, outcome: Outcome, rationale: str) -> None:
        self._dq.append(
            ArchiveEntry(
                ts=time.time(),
                failure_class=failure_class,
                outcome=outcome,
                rationale=rationale,
            )
        )

    def dead_ends(self) -> set[str]:
        """Failure classes whose last recorded attempt was rejected — do not
        spend coding-tier budget re-deriving these."""
        last: dict[str, Outcome] = {}
        for e in self._dq:
            last[e.failure_class] = e.outcome
        return {fc for fc, o in last.items() if o == "rejected"}

    def __len__(self) -> int:
        return len(self._dq)

    def all(self) -> list[ArchiveEntry]:
        return list(self._dq)
