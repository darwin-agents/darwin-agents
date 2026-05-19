"""Fitness — economic survival, scored (docs/engineering/06).

    fitness = α·normalized_profit
            + β·strategy_diversity_bonus
            + γ·escalation_quality_score
            − δ·policy_violation_penalty

The weights live in the DNA hyperparameters and are **deliberately not
evolvable**: letting a Darwin reweight its own fitness is the obvious
wireheading path (06, 10). This module reads them; nothing here can change
them, and the evaluation harness that grades self-modifications uses this
same code so a Darwin cannot grade itself by softer rules.

There is intentionally **no volatility term**: ruinous variance is already
selected against by the hardest mechanism in the system — the treasury hitting
zero. Penalising it again would double-count and bias the ecosystem toward
mediocrity (06).
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..hyperparameters import DEFAULT_HYPERPARAMETERS, Hyperparameters


class FitnessWindow(BaseModel):
    """The measured facts over the rolling window. Profit is read from the
    on-chain-reconciled ledger, so a lucky spike right before the check does
    not survive a 7-day window (06: skill survives a window; luck usually
    doesn't)."""

    model_config = {"frozen": True}

    profit_usdc: float  # may be negative
    starting_capital_usdc: float = Field(gt=0)
    distinct_strategies: int = Field(ge=0)
    escalations_approved: int = Field(ge=0)
    escalations_rejected: int = Field(ge=0)
    policy_violations: int = Field(ge=0)


class FitnessScore(BaseModel):
    model_config = {"frozen": True}

    score: float
    normalized_profit: float
    diversity_bonus: float
    escalation_quality: float
    policy_penalty: float


def _escalation_quality(approved: int, rejected: int) -> float:
    """In [-1, 1]. Good escalations (owner said yes → Darwin asked a good
    question) reward; pestering (rejected/ignored) penalises. No history is
    neutral, not rewarded."""
    total = approved + rejected
    if total == 0:
        return 0.0
    return (approved - rejected) / total


def compute_fitness(
    w: FitnessWindow, hp: Hyperparameters = DEFAULT_HYPERPARAMETERS
) -> FitnessScore:
    """Pure. Deterministic. The single definition of 'doing well'."""
    normalized_profit = w.profit_usdc / w.starting_capital_usdc

    # Diversity: a small, saturating reward for sampling >1 archetype, so it
    # nudges away from premature lock-in without ever rivalling profit.
    diversity_bonus = 1.0 - 1.0 / max(1, w.distinct_strategies)

    escalation_quality = _escalation_quality(
        w.escalations_approved, w.escalations_rejected
    )

    policy_penalty = float(w.policy_violations)

    score = (
        hp.fitness_profit_weight * normalized_profit
        + hp.fitness_diversity_weight * diversity_bonus
        + hp.fitness_escalation_quality_weight * escalation_quality
        - hp.fitness_policy_violation_penalty * policy_penalty
    )
    return FitnessScore(
        score=score,
        normalized_profit=normalized_profit,
        diversity_bonus=diversity_bonus,
        escalation_quality=escalation_quality,
        policy_penalty=policy_penalty,
    )
