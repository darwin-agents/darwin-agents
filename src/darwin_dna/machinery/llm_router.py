"""Capability-profile LLM routing (docs/engineering/13, P6).

**No model names in Layer 0.** The genome speaks only in capability tiers; the
platform's LiteLLM gateway maps a tier to whatever concrete model is cheapest-
that-meets-bar today. When a provider dies or doubles its price the platform
updates the routing table and every Darwin adapts with zero code change.

This module is the genome side of that contract: it owns the tier vocabulary,
enforces the per-day tier call caps from the hyperparameters, and delegates
the actual call to an injected transport (real: a LiteLLM virtual key scoped
to this Darwin; fake: a deterministic heuristic responder).
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ..hyperparameters import DEFAULT_HYPERPARAMETERS, Hyperparameters


class Tier(str, Enum):
    """The only way the genome is allowed to ask for intelligence."""

    FAST = "fast"  # cheap, routine (research scouting, classification)
    REASONING = "reasoning"  # the planner; the default thinking tier
    CODING = "coding"  # self-modification; expensive, gated hard
    IMAGE_GEN = "image_gen"
    EMBEDDING = "embedding"


class LLMError(Exception):
    """A routing/transport failure. Surfaced to the loop, never silently
    swallowed — an unanswered plan is a missed heartbeat, not a crash."""


class TierBudgetExceeded(LLMError):
    """The per-day cap for a tier is spent. The planner must back off or
    tier-down rather than burn the budget (docs/engineering/18 failure mode:
    'burns LLM money faster than it earns')."""


class LLMBudgetExhausted(LLMError):
    """The platform proxy refused the call because this Darwin's *virtual-key*
    budget is spent — LiteLLM returns HTTP 429 (docs/engineering/13: "When the
    Darwin's per-key budget runs out, LiteLLM returns 429. The orchestrator
    interprets this as a treasury check failure"). Unlike a transient
    rate-limit 429, retrying another model in the chain cannot help: the budget
    lives on the key, shared across every model. The transport raises this
    *instead of* exhausting the fallback chain, and it propagates as a typed
    treasury signal — the loop records it distinctly so the spend/treasury
    monitor can escalate or trigger the death sequence rather than treating it
    as an ordinary degraded heartbeat."""


@runtime_checkable
class LLMTransport(Protocol):
    """The platform-operated gateway, abstracted. The genome never sees a
    model name, an API key, or a provider — only this method."""

    def complete(self, tier: str, request: dict[str, Any]) -> dict[str, Any]: ...


_TIER_DAILY_CAP_ATTR = {
    Tier.CODING: "tier_coding_max_daily_calls",
    Tier.REASONING: "tier_reasoning_max_daily_calls",
    Tier.FAST: "tier_fast_max_daily_calls",
}


def hp_cap_for_tier(hp: Hyperparameters, tier: Tier) -> int | None:
    """Hyperparameter daily-call cap for ``tier``, or ``None`` if uncapped.
    Public so the platform layer can read the same mapping the router uses
    without importing a module-private symbol."""
    attr = _TIER_DAILY_CAP_ATTR.get(tier)
    return int(getattr(hp, attr)) if attr is not None else None


class LLMRouter:
    """Genome-owned router. Per-day, per-tier call accounting; refuses when a
    tier's daily budget is spent. Counters roll at UTC midnight."""

    def __init__(
        self,
        transport: LLMTransport,
        hp: Hyperparameters = DEFAULT_HYPERPARAMETERS,
        *,
        clock=time.time,
    ):
        if not isinstance(transport, LLMTransport):
            raise TypeError("LLMRouter needs an LLMTransport")
        self._t = transport
        self._hp = hp
        self._clock = clock
        self._day = self._utc_day()
        self._calls: dict[Tier, int] = {t: 0 for t in Tier}
        # Platform-installed throttles (docs/engineering/09 level 2). A tier
        # entry here further tightens that tier's per-day cap; the effective
        # cap is ``min(hp cap, throttle)``. Cleared by the platform when the
        # underlying anomaly clears. The genome enforces; the platform sets.
        self._throttle: dict[Tier, int] = {}

    def _utc_day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _roll_if_new_day(self) -> None:
        today = self._utc_day()
        if today != self._day:
            self._day = today
            self._calls = {t: 0 for t in Tier}

    def remaining(self, tier: Tier) -> int | None:
        """Calls left for ``tier`` today, or ``None`` if uncapped. The
        effective cap is the lower of the hyperparameter cap and any
        platform-installed throttle (doc 09 level 2)."""
        self._roll_if_new_day()
        attr = _TIER_DAILY_CAP_ATTR.get(tier)
        if attr is None and tier not in self._throttle:
            return None
        hp_cap = getattr(self._hp, attr) if attr is not None else float("inf")
        cap = min(hp_cap, self._throttle.get(tier, hp_cap))
        return max(0, int(cap) - self._calls[tier])

    def set_throttle(self, tier: Tier, max_daily_calls: int) -> None:
        """Install a tighter per-day cap on ``tier``. Cannot loosen the
        hyperparameter cap (``remaining`` takes the min). Cleared with
        :meth:`clear_throttle`."""
        if max_daily_calls < 0:
            raise ValueError("max_daily_calls must be non-negative")
        self._throttle[tier] = int(max_daily_calls)

    def clear_throttle(self, tier: Tier | None = None) -> None:
        """Remove the platform-installed throttle for ``tier`` (or all
        tiers when ``tier is None``). Does not affect the hyperparameter
        cap or the day's already-spent calls."""
        if tier is None:
            self._throttle.clear()
        else:
            self._throttle.pop(tier, None)

    def throttles(self) -> dict[str, int]:
        """Snapshot of installed throttles for ops/dashboards."""
        return {t.value: cap for t, cap in self._throttle.items()}

    def _check_cap(self, tier: Tier) -> None:
        self._roll_if_new_day()
        rem = self.remaining(tier)
        if rem is not None and rem <= 0:
            raise TierBudgetExceeded(
                f"tier {tier.value} daily call cap exhausted; back off or tier-down"
            )

    def complete(self, tier: Tier, request: dict[str, Any]) -> dict[str, Any]:
        """Route one request at ``tier``. Enforces the cap, then delegates."""
        self._check_cap(tier)
        try:
            resp = self._t.complete(tier.value, request)
        except LLMError:
            # Already a typed routing signal (e.g. LLMBudgetExhausted) — let it
            # propagate with its class intact so the loop can tell a treasury
            # 429 apart from a generic transport hiccup. Rewrapping would flatten
            # every failure to a bare LLMError and lose that distinction.
            raise
        except Exception as exc:
            raise LLMError(f"transport failed for tier {tier.value}: {exc}") from exc
        self._calls[tier] += 1
        return resp

    async def acomplete(
        self, tier: Tier, request: dict[str, Any]
    ) -> dict[str, Any]:
        """Native-async route. Used by the genome's nodes/neurons in live
        operation so an LLM call never blocks the event loop the supervisor
        shares across Darwins (BUG-drive-loop-tick1-freeze). Same cap
        accounting as :meth:`complete`. Delegates to the transport's
        ``acomplete`` when it has one (the real gateway); otherwise falls back
        to the synchronous ``complete`` — fine for the deterministic fake,
        which is instant and never touches the loop or network."""
        self._check_cap(tier)
        try:
            atransport = getattr(self._t, "acomplete", None)
            if atransport is not None:
                resp = await atransport(tier.value, request)
            else:
                resp = self._t.complete(tier.value, request)
        except LLMError:
            # See complete(): preserve typed signals like LLMBudgetExhausted.
            raise
        except Exception as exc:
            raise LLMError(f"transport failed for tier {tier.value}: {exc}") from exc
        self._calls[tier] += 1
        return resp


async def acomplete(llm: Any, tier: Tier, request: dict[str, Any]) -> dict[str, Any]:
    """Call the non-blocking ``acomplete`` when the LLM object exposes it
    (the production :class:`LLMRouter`), else fall back to the synchronous
    ``complete``. Genome nodes and neurons route through this so they stay
    non-blocking on the real stack without forcing every test double / fake
    router to grow an async method. The fallback call is synchronous and
    returns immediately for fakes, so awaiting it is a no-op cost."""
    afn = getattr(llm, "acomplete", None)
    if afn is not None:
        return await afn(tier, request)
    return llm.complete(tier, request)
