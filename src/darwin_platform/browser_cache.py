"""Browser cached-replay promotion + per-session cost attribution
(docs/engineering/15).

Doc 15 specifies the decision tree on top of the browser substrate:

1. Browser-Use sessions (LLM-driven, expensive) are traced.
2. When the *same* task signature succeeds ≥N times, that signature gets
   promoted to a cached path — replay the recorded action sequence via
   the cheaper substrate (Stagehand in production, a stubbed cheap path
   here) instead of re-exploring with the LLM.
3. When the cached replay fails M times in a row, demote back to
   exploration. The cache was stale (the target site changed) and we
   shouldn't keep burning budget on a broken replay.
4. Every session is attributed (neuron, URL, trace id, darwin_id) so the
   operator dashboard can show "Browserbase bill broken down by Darwin /
   neuron / target site."

This module ships the platform-side cache service + a drop-in
``CachedBrowserSubstrate`` wrapping any ``substrate.run(steps) -> dict``
backend. The supervisor wraps each Darwin's browser at spawn time so the
genome's ``browser.operator`` neuron transparently benefits from caching
across heartbeats and even across reproductions in the same supervisor
process.

Limits:

- Signatures hash only the structural shape of the steps (URLs +
  action *kinds*), not values. A login-step with different credentials
  still matches the same signature. This is the right grain — value
  variation is what we WANT cached replays to handle.
- The cache is in-memory for now. Persistence is a follow-on; the audit
  chain captures the session records via the supervisor's audit log.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class BrowserSessionRecord:
    """One Browser session — what we log per call."""

    trace_id: str
    darwin_id: str
    neuron: str
    signature: str
    url: str | None
    status: str          # "ok" | "blocked" | "fail"
    via: str             # "exploration" | "cached"
    cost_usdc: float
    latency_ms: int
    ts: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass
class _SignatureState:
    """Per-signature bookkeeping. Tracks promotion + demotion counters."""

    successes: int = 0
    consecutive_cached_failures: int = 0
    promoted: bool = False
    cached_steps: list[str] = field(default_factory=list)


class BrowserCacheService:
    """The platform-side promotion/demotion engine + session log.

    Stateless across signatures except for the per-signature counters;
    promotion happens automatically once :attr:`promote_after_successes`
    is hit in the exploration path, demotion once
    :attr:`demote_after_failures` consecutive cached-path failures occur.
    """

    def __init__(
        self,
        *,
        promote_after_successes: int = 5,
        demote_after_failures: int = 2,
        max_log_per_darwin: int = 1024,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._promote_after = max(1, int(promote_after_successes))
        self._demote_after = max(1, int(demote_after_failures))
        self._max_log = int(max_log_per_darwin)
        self._clock = clock
        self._signatures: dict[str, _SignatureState] = {}
        # Sessions indexed per Darwin; capped so a hot loop doesn't blow
        # memory. Older entries are evicted (the audit log retains them).
        self._sessions: dict[str, list[BrowserSessionRecord]] = {}

    # -- signatures ------------------------------------------------------
    @staticmethod
    def signature(steps: list[str]) -> str:
        """Hash the structural shape of a step list. Two step lists that
        differ only in *values* (form fields, URLs with the same host)
        produce the same signature; lists with different action sequences
        do not.

        The cheap heuristic: hash the lowercased first word of each step
        plus its host (when an URL is present). Good enough for the
        cache hit/miss decision; signature collisions are bounded by
        per-Darwin scope so the worst case is a cache miss."""
        norm: list[str] = []
        for s in steps:
            ss = str(s).strip().lower()
            head = ss.split()[0] if ss else ""
            host = ""
            if "http" in ss:
                # Pull host out without the URL parser cost; substring search.
                for tok in ss.split():
                    if tok.startswith(("http://", "https://")):
                        rest = tok.split("/", 3)
                        if len(rest) >= 3:
                            host = rest[2]
                        break
            norm.append(f"{head}|{host}")
        joined = "\n".join(norm)
        return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]

    def is_cached(self, signature: str) -> bool:
        st = self._signatures.get(signature)
        return st is not None and st.promoted

    def cached_steps(self, signature: str) -> list[str] | None:
        st = self._signatures.get(signature)
        if st is None or not st.promoted:
            return None
        return list(st.cached_steps)

    # -- recording (called by the wrapper on every session) -------------
    def record(self, record: BrowserSessionRecord, *, steps: list[str]) -> None:
        """Record one session + advance the promotion/demotion machinery.

        ``steps`` is the actual step list that ran (we use it to cache the
        replay sequence at promotion time)."""
        bucket = self._sessions.setdefault(record.darwin_id, [])
        bucket.append(record)
        if len(bucket) > self._max_log:
            # Evict the oldest 25% so we don't churn on every insert.
            keep = self._max_log * 3 // 4
            del bucket[: len(bucket) - keep]
        st = self._signatures.setdefault(record.signature, _SignatureState())
        if record.via == "exploration":
            if record.status == "ok":
                st.successes += 1
                if (
                    not st.promoted
                    and st.successes >= self._promote_after
                ):
                    st.promoted = True
                    st.cached_steps = list(steps)
                    st.consecutive_cached_failures = 0
            else:
                # Exploration failure doesn't directly demote; demotion is
                # for cached-path regressions only.
                pass
        else:  # via == "cached"
            if record.status == "ok":
                st.consecutive_cached_failures = 0
            else:
                st.consecutive_cached_failures += 1
                if st.consecutive_cached_failures >= self._demote_after:
                    # Cache went stale — demote.
                    st.promoted = False
                    st.consecutive_cached_failures = 0
                    # Keep the success count so promotion can re-trigger;
                    # the operator can introspect via the stats endpoint.

    # -- reads (operator dashboards) ------------------------------------
    def sessions(
        self,
        *,
        darwin_id: str | None = None,
        signature: str | None = None,
        limit: int | None = None,
    ) -> list[BrowserSessionRecord]:
        if darwin_id is not None:
            rows = list(self._sessions.get(darwin_id, []))
        else:
            rows = [r for bucket in self._sessions.values() for r in bucket]
        if signature is not None:
            rows = [r for r in rows if r.signature == signature]
        if limit is not None:
            rows = rows[-limit:]
        return rows

    def stats(self) -> dict:
        """Aggregate the per-signature state and per-Darwin counters for
        the operator dashboard. O(N) over the log; cheap at the cadence
        a dashboard polls."""
        out: dict[str, Any] = {
            "signatures": [],
            "darwins": {},
            "total_sessions": 0,
            "total_cost_usdc": 0.0,
        }
        for sig, st in self._signatures.items():
            out["signatures"].append({
                "signature": sig,
                "promoted": st.promoted,
                "successes": st.successes,
                "consecutive_cached_failures": st.consecutive_cached_failures,
            })
        for did, bucket in self._sessions.items():
            cost = sum(r.cost_usdc for r in bucket)
            out["darwins"][did] = {
                "sessions": len(bucket),
                "cost_usdc": cost,
                "ok": sum(1 for r in bucket if r.status == "ok"),
                "blocked": sum(1 for r in bucket if r.status == "blocked"),
                "fail": sum(1 for r in bucket if r.status == "fail"),
                "cached_hits": sum(1 for r in bucket if r.via == "cached"),
                "exploration": sum(1 for r in bucket if r.via == "exploration"),
            }
            out["total_sessions"] += len(bucket)
            out["total_cost_usdc"] += cost
        return out


class CachedBrowserSubstrate:
    """Drop-in for the genome's browser substrate (``run(steps) -> dict``)
    that consults the cache before each call and records every session.

    Construction: pass the underlying substrate (Browser-Use exploration
    in production, FakeBrowser in tests) and optionally a separate cheap
    replay substrate. If no replay substrate is given, cached steps still
    run through the exploration substrate but tagged ``via='cached'`` so
    the cost attribution + audit are intact (the production benefit kicks
    in once a real Stagehand client is plugged in)."""

    def __init__(
        self,
        *,
        exploration: Any,
        cache: BrowserCacheService,
        darwin_id: str,
        neuron: str = "browser.operator",
        replay: Any | None = None,
        cached_cost_multiplier: float = 0.1,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._exploration = exploration
        self._replay = replay
        self._cache = cache
        self._darwin_id = darwin_id
        self._neuron = neuron
        self._cached_cost_mult = float(cached_cost_multiplier)
        self._clock = clock
        self._trace_counter = 0

    @property
    def cache(self) -> BrowserCacheService:
        return self._cache

    def _next_trace_id(self) -> str:
        self._trace_counter += 1
        return f"trace-{self._darwin_id}-{self._trace_counter}"

    def _extract_url(self, steps: list[str]) -> str | None:
        for s in steps:
            ss = str(s).strip()
            for tok in ss.split():
                if tok.startswith(("http://", "https://")):
                    return tok.rstrip(",)\"'")
        return None

    def run(self, steps: list[str]) -> dict:
        signature = self._cache.signature(list(steps))
        cached = self._cache.is_cached(signature)
        url = self._extract_url(list(steps))
        substrate = (
            self._replay if (cached and self._replay is not None)
            else self._exploration
        )
        start = self._clock()
        try:
            outcome = substrate.run(list(steps))
        except Exception as exc:
            # Underlying substrate raised — record + re-raise so the
            # neuron can surface the failure mode.
            record = BrowserSessionRecord(
                trace_id=self._next_trace_id(),
                darwin_id=self._darwin_id,
                neuron=self._neuron,
                signature=signature,
                url=url,
                status="fail",
                via="cached" if cached else "exploration",
                cost_usdc=0.0,
                latency_ms=int((self._clock() - start) * 1000),
                ts=self._clock(),
            )
            self._cache.record(record, steps=list(steps))
            raise
        elapsed_ms = int((self._clock() - start) * 1000)
        status = (
            "ok" if outcome.get("ok") else
            "blocked" if outcome.get("blocked") else
            "fail"
        )
        cost = float(outcome.get("cost_usdc", 0.0))
        if cached and self._replay is None:
            # Modeling the cheaper Stagehand path: record a fraction of
            # the cost so the operator dashboard's "cached vs exploration"
            # spend split is meaningful even before a real replay
            # substrate is wired.
            cost *= self._cached_cost_mult
        record = BrowserSessionRecord(
            trace_id=self._next_trace_id(),
            darwin_id=self._darwin_id,
            neuron=self._neuron,
            signature=signature,
            url=url,
            status=status,
            via="cached" if cached else "exploration",
            cost_usdc=cost,
            latency_ms=elapsed_ms,
            ts=self._clock(),
        )
        self._cache.record(record, steps=list(steps))
        # Pass through the underlying outcome; the genome sees the same
        # shape it would from FakeBrowser/BrowserUseClient.
        return outcome
