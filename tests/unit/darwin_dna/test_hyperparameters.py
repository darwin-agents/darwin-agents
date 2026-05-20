"""Hyperparameter & hard-cap validator tests (docs/engineering/18).

The genome ships two tiers:

* ``HardCaps`` — frozen, inviolable. Construction must refuse anything
  out of order or non-positive.
* ``Hyperparameters`` — soma-tunable within bounds. The validator enforces
  heartbeat ordering, the profit-transfer fraction is a probability, and
  the policy-violation penalty is non-negative.
"""

from __future__ import annotations

import pytest

from darwin_dna.hyperparameters import (
    DEFAULT_HYPERPARAMETERS,
    HARD_CAPS,
    HardCaps,
    Hyperparameters,
)


# -- HardCaps ----------------------------------------------------------------


def test_default_hard_caps_are_ordered():
    assert 0 < HARD_CAPS.per_action_usdc <= HARD_CAPS.per_day_usdc
    assert HARD_CAPS.per_day_usdc <= HARD_CAPS.per_lifetime_usdc


def test_hard_caps_refuse_out_of_order():
    with pytest.raises(Exception):
        HardCaps(per_action_usdc=100.0, per_day_usdc=10.0, per_lifetime_usdc=1_000.0)


def test_hard_caps_refuse_non_positive_per_action():
    with pytest.raises(Exception):
        HardCaps(per_action_usdc=0.0, per_day_usdc=1.0, per_lifetime_usdc=10.0)


def test_hard_caps_are_frozen():
    with pytest.raises(Exception):
        HARD_CAPS.per_action_usdc = 999.0  # type: ignore[misc]


# -- Hyperparameters ---------------------------------------------------------


def test_defaults_satisfy_validators():
    # If the shipped defaults ever violate the validator they were intended
    # to enforce, the genome can't import — pin it.
    assert isinstance(DEFAULT_HYPERPARAMETERS, Hyperparameters)


def test_heartbeat_intervals_must_be_ordered():
    with pytest.raises(Exception):
        Hyperparameters(
            heartbeat_min_interval_seconds=10.0,
            heartbeat_interval_seconds=5.0,  # below min — invalid
            heartbeat_max_interval_seconds=20.0,
        )


def test_profit_transfer_fraction_must_be_a_probability():
    with pytest.raises(Exception):
        Hyperparameters(profit_transfer_fraction=1.5)
    with pytest.raises(Exception):
        Hyperparameters(profit_transfer_fraction=-0.1)


def test_policy_violation_penalty_must_be_non_negative():
    with pytest.raises(Exception):
        Hyperparameters(fitness_policy_violation_penalty=-1.0)


def test_hyperparameters_are_frozen():
    with pytest.raises(Exception):
        DEFAULT_HYPERPARAMETERS.heartbeat_interval_seconds = 1.0  # type: ignore[misc]
