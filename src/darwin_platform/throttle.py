"""Per-Darwin resource throttles for non-LLM substrates (doc 09 level 2).

The genome already has a runtime-installable throttle on the LLM router
(:class:`darwin_dna.machinery.llm_router.LLMRouter.set_throttle`). Doc 09
specifies the same level-2 lever for **sandbox sessions**, **browser
sessions**, and **outbound x402/egress calls** — a sustained WARN should
rate-limit the offending resource, not just emit an alert.

This module ships the platform-side machinery:

- :class:`ThrottleService` — holds per-(darwin_id, kind) throttles and
  per-day counters. The supervisor exposes ``throttle_sandbox`` /
  ``throttle_browser`` / ``throttle_egress`` HTTP routes.
- :class:`ThrottledBrowserSubstrate`, :class:`ThrottledSandboxSubstrate`,
  :class:`ThrottledEgressClient` — drop-in wrappers that consult the
  service before each call. When the daily cap is reached, the wrapper
  returns the substrate's "refused" shape (browser: a navigation_failed
  dict; sandbox: an exit-code-non-zero dict; egress: 429).

Throttles only tighten — installing a throttle higher than the
substrate's own daily cap (if any) does not relax the substrate's cap.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from typing import Any, Callable


THROTTLE_KIND_SANDBOX = "sandbox"
THROTTLE_KIND_BROWSER = "browser"
THROTTLE_KIND_EGRESS = "egress"
THROTTLE_KINDS = frozenset({
    THROTTLE_KIND_SANDBOX, THROTTLE_KIND_BROWSER, THROTTLE_KIND_EGRESS,
})


class ThrottleError(Exception):
    """Invalid kind / invalid limit."""


@dataclass(frozen=True)
class ResourceThrottle:
    """One installed throttle. Frozen — an update is a replace."""

    darwin_id: str
    kind: str
    max_daily_calls: int
    installed_at: float

    def to_json(self) -> dict:
        return asdict(self)


class ThrottleService:
    """Holds the per-Darwin throttles + per-day counters.

    A counter is bucketed by UTC day so a slow-rolling cap auto-resets
    at midnight (matching the genome's wallet ``_roll()`` convention).
    Tests inject a fake clock to exercise rollovers."""

    def __init__(self, *, clock: Callable[[], float] = time.time) -> None:
        self._clock = clock
        self._throttles: dict[tuple[str, str], ResourceThrottle] = {}
        # (darwin_id, kind, day_str) -> count
        self._counts: dict[tuple[str, str, str], int] = {}

    @staticmethod
    def _validate_kind(kind: str) -> None:
        if kind not in THROTTLE_KINDS:
            raise ThrottleError(
                f"unknown throttle kind {kind!r}; "
                f"expected one of {sorted(THROTTLE_KINDS)}"
            )

    def _day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def set(
        self,
        *,
        darwin_id: str,
        kind: str,
        max_daily_calls: int,
    ) -> ResourceThrottle:
        self._validate_kind(kind)
        if max_daily_calls < 0:
            raise ThrottleError("max_daily_calls must be >= 0")
        t = ResourceThrottle(
            darwin_id=darwin_id,
            kind=kind,
            max_daily_calls=int(max_daily_calls),
            installed_at=self._clock(),
        )
        self._throttles[(darwin_id, kind)] = t
        return t

    def clear(
        self,
        *,
        darwin_id: str,
        kind: str | None = None,
    ) -> list[ResourceThrottle]:
        """Clear one throttle (when ``kind`` is given) or every throttle
        for this Darwin. Returns the removed records."""
        removed: list[ResourceThrottle] = []
        if kind is not None:
            self._validate_kind(kind)
            t = self._throttles.pop((darwin_id, kind), None)
            if t is not None:
                removed.append(t)
        else:
            for k in list(self._throttles.keys()):
                if k[0] == darwin_id:
                    removed.append(self._throttles.pop(k))
        return removed

    def list_for(self, darwin_id: str) -> list[ResourceThrottle]:
        return [
            t for (did, _), t in self._throttles.items()
            if did == darwin_id
        ]

    def get(self, *, darwin_id: str, kind: str) -> ResourceThrottle | None:
        self._validate_kind(kind)
        return self._throttles.get((darwin_id, kind))

    def consume(self, *, darwin_id: str, kind: str) -> bool:
        """Try to record one call against the throttle. Returns True if
        allowed (counter was incremented), False if the cap is reached.
        Callers MUST treat False as a refusal — do not invoke the
        substrate."""
        self._validate_kind(kind)
        t = self._throttles.get((darwin_id, kind))
        if t is None:
            return True  # no throttle installed → unlimited (subject to
                         # the substrate's own caps)
        day = self._day()
        key = (darwin_id, kind, day)
        used = self._counts.get(key, 0)
        if used >= t.max_daily_calls:
            return False
        self._counts[key] = used + 1
        return True

    def used_today(self, *, darwin_id: str, kind: str) -> int:
        self._validate_kind(kind)
        return self._counts.get((darwin_id, kind, self._day()), 0)


# ---------------------------------------------------------------------------
# Substrate wrappers
# ---------------------------------------------------------------------------

class ThrottledBrowserSubstrate:
    """Wraps any ``substrate.run(steps) -> dict`` substrate; refuses the
    call when the daily browser cap is reached. Cooperates with the
    :class:`CachedBrowserSubstrate` — wrap either side; a typical wiring
    nests the throttle OUTSIDE the cache so cached replays still count
    against the operator-installed cap (the doc-09 intent is "stop
    burning provider budget", not "stop the LLM-driven path only")."""

    def __init__(
        self,
        *,
        inner: Any,
        throttle: ThrottleService,
        darwin_id: str,
    ) -> None:
        self._inner = inner
        self._throttle = throttle
        self._darwin_id = darwin_id

    def run(self, steps: list[str]) -> dict:
        ok = self._throttle.consume(
            darwin_id=self._darwin_id, kind=THROTTLE_KIND_BROWSER,
        )
        if not ok:
            return {
                "ok": False,
                "blocked": "throttled",
                "detail": "browser daily throttle reached",
                "cost_usdc": 0.0,
            }
        return self._inner.run(list(steps))


class ThrottledSandboxSubstrate:
    """Wraps any sandbox client; refuses ``run_code`` when the daily
    sandbox cap is reached. The refused return shape matches the existing
    E2BSandbox's error shape so neurons handle the cap as "execution
    refused" rather than crashing."""

    def __init__(
        self,
        *,
        inner: Any,
        throttle: ThrottleService,
        darwin_id: str,
    ) -> None:
        self._inner = inner
        self._throttle = throttle
        self._darwin_id = darwin_id

    def run_code(
        self,
        *,
        language: str,
        code: str,
        timeout_seconds: float = 30.0,
    ) -> dict:
        ok = self._throttle.consume(
            darwin_id=self._darwin_id, kind=THROTTLE_KIND_SANDBOX,
        )
        if not ok:
            return {
                "ok": False,
                "exit_code": -1,
                "stdout": "",
                "stderr": "sandbox daily throttle reached",
                "cost_usdc": 0.0,
            }
        return self._inner.run_code(
            language=language, code=code, timeout_seconds=timeout_seconds,
        )


class ThrottledEgressClient:
    """Wraps an x402 (or any) HTTP client; refuses ``request`` calls when
    the daily egress cap is reached. Returns a 429-shaped response so the
    caller's existing 4xx handling kicks in."""

    def __init__(
        self,
        *,
        inner: Any,
        throttle: ThrottleService,
        darwin_id: str,
    ) -> None:
        self._inner = inner
        self._throttle = throttle
        self._darwin_id = darwin_id

    def request(self, method: str, url: str, **kwargs):
        ok = self._throttle.consume(
            darwin_id=self._darwin_id, kind=THROTTLE_KIND_EGRESS,
        )
        if not ok:
            import types
            return types.SimpleNamespace(
                status_code=429,
                headers={"X-Throttle-Reason": "egress daily throttle reached"},
                text="",
            )
        return self._inner.request(method, url, **kwargs)
