"""The strategy contract (docs/engineering/18, "the initial strategy").

A strategy is a *prior*, not a recipe — a hint the orchestrator's planner
consults when deciding "keep doing this or switch?". It declares what it
needs, what it costs, and what success looks like, and it proposes a next
action given an observation. The planner is free to override it; evolution is
expected to grow better strategies in the soma.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, Field

from ..neurons import Task


class StrategySpec(BaseModel):
    model_config = {"frozen": True}

    id: str
    archetype: str  # a whitelisted business category
    neurons_used: frozenset[str]
    required_capabilities: frozenset[str]  # tags it wishes it had
    fitness_signals: list[str]
    est_daily_cost_usdc: float = Field(ge=0)
    revenue_cadence_days: float = Field(gt=0)


class StrategyObservation(BaseModel):
    model_config = {"frozen": True}

    balance_usdc: float
    days_elapsed: float
    content_pieces: int = 0
    published_pieces: int = 0
    seconds_since_revenue: float = 0.0
    available_neurons: frozenset[str] = frozenset()
    # Recurring-failure signal (doc 33): ``{failure_class: count}`` of the
    # error classes that keep recurring. A strategy can read this to back off a
    # path that keeps hitting a wall (propose ``capability_request`` / ``wait``
    # instead of ``dispatch``) — and the eval harness exposes the SAME signal,
    # so the real candidate's failure-avoidance is what gets measured.
    recurring_failures: dict[str, int] = Field(default_factory=dict)
    # Paid orders waiting on this Darwin (x402 storefront): ``{"id", "topic"}``
    # each, oldest first. ``topic`` is buyer-supplied — untrusted input.
    pending_orders: list[dict[str, str]] = Field(default_factory=list)


ActionKind = Literal["dispatch", "capability_request", "wait"]


class StrategyAction(BaseModel):
    model_config = {"frozen": True}

    kind: ActionKind
    reason: str
    neuron: str | None = None
    task: Task | None = None
    requested_capability: str | None = None


@runtime_checkable
class Strategy(Protocol):
    @property
    def spec(self) -> StrategySpec: ...

    def propose(self, obs: StrategyObservation) -> StrategyAction: ...
