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
