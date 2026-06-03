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
    # Owner/supervisor pause (docs/engineering/09 level-3). Distinct from
    # death: a paused Darwin is still alive — it halts at a checkpoint and
    # resumes the same life when the flag clears. Deterministically False
    # for any run nobody pauses, so it does not perturb cross-engine parity.
    paused: bool = False
    death_reason: str | None = None
    route: str = "perceive"  # set by each node for the next edge

    # economics / environment (filled by perceive)
    balance_usdc: float = 0.0
    # Non-USDC positions marked to USDC at reconciliation (doc 05 §Treasury).
    # ``balance_usdc`` is cash only; a trading Darwin's inventory lives here so
    # the bankruptcy check can see net worth (cash + holdings) and not kill a
    # Darwin that is cash-poor but holding-rich. 0.0 (the default) when a Darwin
    # holds nothing — then net worth == cash and behaviour is unchanged.
    holdings_value_usdc: float = 0.0
    net_worth_usdc: float = 0.0  # balance_usdc + holdings_value_usdc, for display
    days_elapsed: float = 0.0
    content_pieces: int = 0
    published_pieces: int = 0
    seconds_since_revenue: float = 0.0
    available_neurons: list[str] = Field(default_factory=list)

    # planning
    plan: dict[str, Any] = Field(default_factory=dict)  # the chosen action
    last_action: str | None = None
    # World-derived text the planner may see — ALREADY fenced as untrusted at
    # the dispatch boundary (docs/engineering/10). Bounded; never raw.
    world_observations: list[str] = Field(default_factory=list)

    # reflection / gates
    dispatches_done: int = 0
    successful_dispatches: int = 0
    recurring_failures: dict[str, int] = Field(default_factory=dict)
    distinct_strategies: list[str] = Field(default_factory=list)
    mutation_gate_open: bool = False
    pending_capability_requests: list[str] = Field(default_factory=list)
    pending_escalations: list[str] = Field(default_factory=list)

    # escalation pause/resume (P4, docs/engineering/08). A blocked unit of
    # work does not fail — it parks here until the owner answers, then the
    # exact task is re-dispatched (resumed). Keyed by ticket id.
    open_escalations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    resume_queue: list[dict[str, Any]] = Field(default_factory=list)

    # fitness counter-pressures, measured over the window since the last
    # reproduction (reset there, like profit). These feed the anti-wireheading
    # terms in fitness (docs/engineering/06, 10) — they must carry real data.
    escalations_approved: int = 0
    escalations_rejected: int = 0
    policy_violations: int = 0

    # evolution
    profit_baseline_usdc: float = 0.0  # balance at last reproduction
    starting_capital_usdc: float = 0.0
    reproduce: bool = False
    # Platform-set force flag (doc 08 POST /darwins/{id}/reproduce/force;
    # doc 02 forced amendment reproduction). When True, ``check_reproduction``
    # bypasses the fitness + profit gate and lets reproduction fire on the
    # next loop boundary. Consumed (reset to False) after firing.
    force_reproduce: bool = False
    fitness: float | None = None

    # owner runtime controls (docs/engineering/09). The supervisor bridges
    # these into working memory at each heartbeat boundary; the genome nodes
    # honour them. All default to the inert value so a Darwin nobody
    # constrains behaves exactly as before — cross-engine parity undisturbed.
    # - reproduction_blocked: check_reproduction never fires a *natural*
    #   spawn (a platform forced reproduction still overrides — forcing is an
    #   explicit override, blocking is a standing policy).
    # - disabled_neurons / disabled_categories: dispatch drops a disabled
    #   neuron from selection and refuses to dispatch a disabled category.
    reproduction_blocked: bool = False
    disabled_neurons: list[str] = Field(default_factory=list)
    disabled_categories: list[str] = Field(default_factory=list)

    # diagnostics — recent node trail for the dashboard / tests
    trail: list[str] = Field(default_factory=list)

    def note(self, node: str) -> None:
        self.trail.append(f"{self.tick}:{node}")
        # bounded — working memory is not an audit log
        if len(self.trail) > 200:
            del self.trail[:-200]
