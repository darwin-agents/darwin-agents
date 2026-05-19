"""The shared state object the graph carries (docs/engineering/03, 12).

This is *working memory*: current plan, in-flight intent, recent observations,
the gate counters. In production it lives in the LangGraph checkpoint store
(Postgres) and is reconstructed after a sandbox replacement. Here it is a
plain, JSON-round-trippable model so the internal runner and a real LangGraph
can carry the same shape.

The founding graph is intentionally simple. Sophistication is the soma's job
(docs/engineering/18).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class OrchestratorState(BaseModel):
    model_config = {"validate_assignment": True}

    # lifecycle
    tick: int = 0
    alive: bool = True
    halt: bool = False
    death_reason: str | None = None
    route: str = "perceive"  # set by each node for the next edge

    # economics / environment (filled by perceive)
    balance_usdc: float = 0.0
    days_elapsed: float = 0.0
    content_pieces: int = 0
    published_pieces: int = 0
    seconds_since_revenue: float = 0.0
    available_neurons: list[str] = Field(default_factory=list)

    # planning
    plan: dict[str, Any] = Field(default_factory=dict)  # the chosen action
    last_action: str | None = None

    # reflection / gates
    dispatches_done: int = 0
    successful_dispatches: int = 0
    recurring_failures: dict[str, int] = Field(default_factory=dict)
    distinct_strategies: list[str] = Field(default_factory=list)
    mutation_gate_open: bool = False
    pending_capability_requests: list[str] = Field(default_factory=list)
    pending_escalations: list[str] = Field(default_factory=list)

    # evolution
    profit_baseline_usdc: float = 0.0  # balance at last reproduction
    starting_capital_usdc: float = 0.0
    reproduce: bool = False
    fitness: float | None = None

    # diagnostics — recent node trail for the dashboard / tests
    trail: list[str] = Field(default_factory=list)

    def note(self, node: str) -> None:
        self.trail.append(f"{self.tick}:{node}")
        # bounded — working memory is not an audit log
        if len(self.trail) > 200:
            del self.trail[:-200]
