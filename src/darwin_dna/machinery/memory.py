"""Episodic + semantic memory (docs/engineering/03, "State, memory, and
persistence").

A Darwin has three kinds of memory. *Working memory* is the orchestrator
state object (``state.py``). The other two live here:

- **Episodic memory** — an append-only log of "I did X and Y happened",
  consulted when the planner wants to know *"have I seen this before?"*
  (doc 03). Production stores it in a per-Darwin LanceDB with embeddings; the
  genome ships the *deterministic* form: exact-signature recall with a
  cheap token-overlap fallback. Embeddings/vector search are a soma/infra
  refinement — they cannot go in the DNA without a model lock-in (P6) and a
  heavy dependency (P9). Bounded: working memory is not the audit log; the
  hash-chained log is the durable record.

- **Semantic memory** — the *staging ground for lessons* (doc 03). When the
  reflect node decides a runtime observation is a real, structured lesson it
  stages it here. At the next reproduction the distiller drains the staging
  ground and the strongest-evidence lessons get promoted into a sealed DNA
  layer the child inherits forever. Staging is what lets a lesson survive
  even when the transient counter that produced it (e.g. a recurring-failure
  tally) was cleared before reproduction — that is the whole point of a
  staging ground.

Both are rebuilt fresh on a sandbox-replacement resume, like the ledger and
the mutation archive: the durable, replayable record of a life is the audit
chain, not these in-life caches.
"""

from __future__ import annotations

import re
import time
from collections import deque
from typing import Any

from pydantic import BaseModel, Field

# A "signature" is the same stable identity dispatch uses for a unit of work
# (``neuron|task.kind|tags``). Recall is deterministic: exact-signature hits
# first, then a cheap token-overlap score so "have I seen something *like*
# this?" works without embeddings.
_TOKEN_RE = re.compile(r"[a-z0-9_.]+")


def _tokens(s: str) -> frozenset[str]:
    return frozenset(_TOKEN_RE.findall(s.lower()))


class EpisodicRecord(BaseModel):
    """One "I did X and Y happened" row. Legible on purpose — a human or a
    descendant reading the audit trail must be able to follow it."""

    model_config = {"frozen": True}

    tick: int
    ts: float
    signature: str  # neuron|kind|tags — the dispatch identity
    neuron: str
    outcome: str  # "ok" | "escalated" | "failed"
    error_code: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)


class EpisodicMemory:
    """Append-only, bounded ring of outcomes. Append-only from the caller's
    side; the oldest rows fall off at the cap (doc 03: working memory is not
    an audit log)."""

    def __init__(self, max_entries: int):
        self._dq: deque[EpisodicRecord] = deque(maxlen=max_entries)

    def record(
        self,
        *,
        tick: int,
        signature: str,
        neuron: str,
        outcome: str,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        self._dq.append(
            EpisodicRecord(
                tick=tick,
                ts=time.time() if ts is None else ts,
                signature=signature,
                neuron=neuron,
                outcome=outcome,
                error_code=error_code,
                detail=detail or {},
            )
        )

    def recall(self, signature: str, *, limit: int = 5) -> list[EpisodicRecord]:
        """"Have I seen this before?" — newest first. Exact-signature matches
        rank above token-overlap neighbours; ties break by recency. Pure and
        deterministic so a 90-day sim is reproducible."""
        if not self._dq:
            return []
        want = _tokens(signature)
        scored: list[tuple[float, int, EpisodicRecord]] = []
        for i, rec in enumerate(self._dq):
            if rec.signature == signature:
                score = 1.0
            else:
                have = _tokens(rec.signature)
                union = want | have
                score = (len(want & have) / len(union)) if union else 0.0
            if score > 0.0:
                scored.append((score, i, rec))
        scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
        return [rec for _, _, rec in scored[: max(0, limit)]]

    def __len__(self) -> int:
        return len(self._dq)

    def all(self) -> list[EpisodicRecord]:
        return list(self._dq)


class StagedLesson(BaseModel):
    """A candidate lesson observed at runtime, not yet sealed into DNA. Same
    shape the distiller emits (``kind/scope/statement/payload``) so promotion
    is a straight, legible lift — never an opaque transform."""

    model_config = {"validate_assignment": True}

    kind: str  # "avoid" | "prefer" (mirrors layers.Lesson.kind)
    scope: str  # the dedupe key, e.g. "failure:rate_limited"
    statement: str
    payload: dict[str, Any] = Field(default_factory=dict)
    evidence: int = 1  # times observed — strength for promotion ordering
    first_ts: float
    last_ts: float


class SemanticMemory:
    """The lesson staging ground (doc 03). The reflect node stages structured
    candidates; ``drain`` hands them to the distiller at reproduction. Bounded
    and deduped by ``scope`` so a hot loop cannot flood it — repeated evidence
    *strengthens* an entry, it does not multiply it."""

    def __init__(self, max_staged: int):
        self._max = max_staged
        self._by_scope: dict[str, StagedLesson] = {}

    def stage(
        self,
        *,
        kind: str,
        scope: str,
        statement: str,
        payload: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        now = time.time() if ts is None else ts
        existing = self._by_scope.get(scope)
        if existing is not None:
            # Same lesson, more evidence: strengthen, keep the latest framing.
            self._by_scope[scope] = existing.model_copy(
                update={
                    "evidence": existing.evidence + 1,
                    "statement": statement,
                    "payload": payload or existing.payload,
                    "last_ts": now,
                }
            )
            return
        if len(self._by_scope) >= self._max:
            # Bounded: evict the weakest, oldest staged candidate so a new
            # observation can never be silently dropped under pressure.
            weakest = min(
                self._by_scope.values(), key=lambda s: (s.evidence, s.last_ts)
            )
            del self._by_scope[weakest.scope]
        self._by_scope[scope] = StagedLesson(
            kind=kind,
            scope=scope,
            statement=statement,
            payload=payload or {},
            evidence=1,
            first_ts=now,
            last_ts=now,
        )

    def staged(self) -> list[StagedLesson]:
        """Strongest evidence first — the order the distiller promotes in."""
        return sorted(
            self._by_scope.values(),
            key=lambda s: (s.evidence, s.last_ts),
            reverse=True,
        )

    def drain(self) -> list[StagedLesson]:
        """Take the staged set and clear it. Called once at a reproduction
        event: the staging ground is windowed per generation, exactly like the
        fitness counter-pressures and the profit baseline."""
        out = self.staged()
        self._by_scope.clear()
        return out

    def __len__(self) -> int:
        return len(self._by_scope)
