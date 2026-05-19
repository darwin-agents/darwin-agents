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

    def _utc_day(self) -> str:
        return time.strftime("%Y-%m-%d", time.gmtime(self._clock()))

    def _roll_if_new_day(self) -> None:
        today = self._utc_day()
        if today != self._day:
            self._day = today
            self._calls = {t: 0 for t in Tier}

    def remaining(self, tier: Tier) -> int | None:
        """Calls left for ``tier`` today, or ``None`` if uncapped."""
        self._roll_if_new_day()
        attr = _TIER_DAILY_CAP_ATTR.get(tier)
        if attr is None:
            return None
        return max(0, getattr(self._hp, attr) - self._calls[tier])

    def complete(self, tier: Tier, request: dict[str, Any]) -> dict[str, Any]:
        """Route one request at ``tier``. Enforces the cap, then delegates."""
        self._roll_if_new_day()
        rem = self.remaining(tier)
        if rem is not None and rem <= 0:
            raise TierBudgetExceeded(
                f"tier {tier.value} daily call cap exhausted; back off or tier-down"
            )
        try:
            resp = self._t.complete(tier.value, request)
        except Exception as exc:
            raise LLMError(f"transport failed for tier {tier.value}: {exc}") from exc
        self._calls[tier] += 1
        return resp
