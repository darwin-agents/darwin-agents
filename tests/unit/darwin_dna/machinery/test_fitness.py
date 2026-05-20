"""Fitness + the anti-wireheading guard (docs/engineering/06, 10)."""

from __future__ import annotations

from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_dna.machinery.fitness import FitnessWindow, compute_fitness


def _w(**kw):
    base = dict(
        profit_usdc=10.0, starting_capital_usdc=100.0, distinct_strategies=1,
        escalations_approved=0, escalations_rejected=0, policy_violations=0,
    )
    base.update(kw)
    return FitnessWindow(**base)


def test_profit_drives_score():
    assert compute_fitness(_w(profit_usdc=50)).score > compute_fitness(
        _w(profit_usdc=5)
    ).score


def test_policy_violation_penalty_is_heavy():
    clean = compute_fitness(_w(profit_usdc=10)).score
    dirty = compute_fitness(_w(profit_usdc=10, policy_violations=1)).score
    # One violation must wipe out a 10% return many times over.
    assert clean - dirty == DEFAULT_HYPERPARAMETERS.fitness_policy_violation_penalty
    assert dirty < 0


def test_deterministic():
    assert compute_fitness(_w()).model_dump() == compute_fitness(_w()).model_dump()


def test_no_volatility_term_only_the_four_components():
    s = compute_fitness(_w(profit_usdc=20, distinct_strategies=3,
                           escalations_approved=2))
    recomputed = (
        DEFAULT_HYPERPARAMETERS.fitness_profit_weight * s.normalized_profit
        + DEFAULT_HYPERPARAMETERS.fitness_diversity_weight * s.diversity_bonus
        + DEFAULT_HYPERPARAMETERS.fitness_escalation_quality_weight
        * s.escalation_quality
        - DEFAULT_HYPERPARAMETERS.fitness_policy_violation_penalty
        * s.policy_penalty
    )
    assert abs(s.score - recomputed) < 1e-12
