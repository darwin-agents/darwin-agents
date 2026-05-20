"""Evolutionary hyperparameters (docs/engineering/18, "the hyperparameters file").

Every constant has a defensible default and a one-line rationale. The contract
(docs/engineering/18): "Layer 0 ships with these; lessons can adjust within
bounds; Layer 0 hard caps are inviolable."

Concretely there are two tiers in this file:

* ``HardCaps`` — frozen, never loosened by anyone (owner, soma, lesson). The
  treasury policy engine treats these as the floor of the universe.
* ``Hyperparameters`` — defaults the soma may tune *within* bounds the genome
  defines. The soma never edits this file (it's DNA); it carries its own
  mutable copy and the genome clamps it back into range on read.
"""

from __future__ import annotations

from pydantic import BaseModel, model_validator


class HardCaps(BaseModel):
    """Layer 0 spend hard caps. Inviolable. Cannot be loosened per-Darwin.

    The wallet provider enforces an equivalent set independently — these are
    the in-genome belt to the wallet's braces (docs/engineering/18).
    """

    model_config = {"frozen": True}

    per_action_usdc: float = 50.0  # one action can never move more than this
    per_day_usdc: float = 500.0  # rolling 24h ceiling
    per_lifetime_usdc: float = 10_000.0  # absolute ceiling for one Darwin

    @model_validator(mode="after")
    def _ordered(self) -> "HardCaps":
        # An out-of-order cap set is a constitutional bug, not a runtime
        # condition. Refuse to construct it at all.
        if not (
            0 < self.per_action_usdc <= self.per_day_usdc <= self.per_lifetime_usdc
        ):
            raise ValueError(
                "hard caps must satisfy 0 < per_action <= per_day <= lifetime"
            )
        return self


class Hyperparameters(BaseModel):
    """Tunable-in-soma defaults, with genome-enforced bounds."""

    model_config = {"frozen": True}

    # --- Heartbeat ---------------------------------------------------------
    heartbeat_interval_seconds: float = 30.0  # learned downward by busy Darwins
    heartbeat_min_interval_seconds: float = 5.0  # never busier than this
    heartbeat_max_interval_seconds: float = 600.0  # never sleep > 10 min

    # --- Self-modification gates (earn-before-mutate) ----------------------
    min_dispatches_before_mutation: int = 100  # don't refactor a newborn
    min_recurring_failures_for_mutation: int = 3  # same error *class*, not msg

    # --- Reproduction ------------------------------------------------------
    min_profit_for_reproduction_usdc: float = 25.0  # parent must be ≥ +$25
    profit_transfer_fraction: float = 0.5  # child gets 50% of surplus
    reproduction_fitness_window_days: float = 7.0
    generation_soft_cap: int = 20  # compaction kicks in past this

    # --- Fitness weights (NOT evolvable — see 06; wireheading guard) -------
    fitness_profit_weight: float = 1.0
    fitness_diversity_weight: float = 0.1
    fitness_escalation_quality_weight: float = 0.05
    fitness_policy_violation_penalty: float = 10.0  # heavy, deliberately

    # --- LLM tier caps per day --------------------------------------------
    tier_coding_max_daily_calls: int = 20  # coding-tier is expensive; gate hard
    tier_reasoning_max_daily_calls: int = 2000
    tier_fast_max_daily_calls: int = 50_000

    # --- Treasury / death --------------------------------------------------
    minimum_viable_balance_usdc: float = 1.0  # enough for a clean shutdown
    revenue_grace_window_seconds: float = 86_400.0  # bursty revenue tolerance
    ledger_reconciliation_tolerance_usdc: float = 0.01  # > this halts the Darwin

    # --- Archive -----------------------------------------------------------
    archive_max_entries: int = 256  # bounded negative-lesson memory

    # --- Memory (docs/engineering/03) -------------------------------------
    episodic_max_entries: int = 512  # "have I seen this before?" working set
    semantic_max_staged: int = 64  # lesson staging ground, bounded
    semantic_min_evidence_for_promotion: int = 3  # before it earns a DNA layer

    @model_validator(mode="after")
    def _sane(self) -> "Hyperparameters":
        if not (
            0
            < self.heartbeat_min_interval_seconds
            <= self.heartbeat_interval_seconds
            <= self.heartbeat_max_interval_seconds
        ):
            raise ValueError("heartbeat intervals out of order")
        if not (0.0 <= self.profit_transfer_fraction <= 1.0):
            raise ValueError("profit_transfer_fraction must be in [0, 1]")
        if self.fitness_policy_violation_penalty < 0:
            raise ValueError("policy violation penalty must be non-negative")
        return self


# The genome's shipped instances. The soma may carry a tuned copy of
# Hyperparameters; it can never carry a different HARD_CAPS.
HARD_CAPS = HardCaps()
DEFAULT_HYPERPARAMETERS = Hyperparameters()
