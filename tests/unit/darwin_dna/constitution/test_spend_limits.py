"""Spend-limit constitution tests (docs/engineering/02, 05).

The hard caps in ``hyperparameters.HARD_CAPS`` are the floor of the
universe — no neuron, no owner, no lesson can loosen them. This file
pins ``effective_caps`` (min of hard and soft) and ``check_within_caps``
against every refusal axis.
"""

from __future__ import annotations

import pytest

from darwin_dna.constitution.spend_limits import (
    CapCheck,
    OwnerSoftCaps,
    check_within_caps,
    effective_caps,
)
from darwin_dna.hyperparameters import HARD_CAPS, HardCaps


# -- effective_caps -----------------------------------------------------------


def test_effective_caps_falls_back_to_hard_when_no_soft():
    out = effective_caps(soft=None, hard=HARD_CAPS)
    assert out == HARD_CAPS


def test_effective_caps_soft_above_hard_is_silently_ignored():
    """Owner can never loosen the genome's ceiling (P2 — controls go down,
    never up)."""
    soft = OwnerSoftCaps(
        per_action_usdc=HARD_CAPS.per_action_usdc * 10,
        per_day_usdc=HARD_CAPS.per_day_usdc * 10,
        per_lifetime_usdc=HARD_CAPS.per_lifetime_usdc * 10,
    )
    out = effective_caps(soft=soft, hard=HARD_CAPS)
    assert out == HARD_CAPS


def test_effective_caps_soft_below_hard_tightens():
    soft = OwnerSoftCaps(per_action_usdc=5.0, per_day_usdc=20.0)
    out = effective_caps(soft=soft, hard=HARD_CAPS)
    assert out.per_action_usdc == 5.0
    assert out.per_day_usdc == 20.0
    # Untightened axis keeps the hard ceiling.
    assert out.per_lifetime_usdc == HARD_CAPS.per_lifetime_usdc


def test_effective_caps_partial_soft_only_tightens_set_axes():
    """Only the axes the owner set are tightened; the rest keep the hard
    ceiling. (Soft must stay ≥ per_action so the resulting HardCaps remains
    well-ordered — that's the constitution's own constraint.)"""
    soft = OwnerSoftCaps(per_day_usdc=HARD_CAPS.per_day_usdc / 2)
    out = effective_caps(soft=soft, hard=HARD_CAPS)
    assert out.per_action_usdc == HARD_CAPS.per_action_usdc
    assert out.per_day_usdc == HARD_CAPS.per_day_usdc / 2
    assert out.per_lifetime_usdc == HARD_CAPS.per_lifetime_usdc


# -- check_within_caps --------------------------------------------------------


def test_check_within_caps_happy_path():
    out = check_within_caps(
        amount_usdc=1.0, spent_today_usdc=0.0, spent_lifetime_usdc=0.0,
    )
    assert out.ok is True
    assert out.ceiling is None
    assert out.reason is None


def test_check_refuses_negative_spend():
    out = check_within_caps(
        amount_usdc=-1.0, spent_today_usdc=0.0, spent_lifetime_usdc=0.0,
    )
    assert out.ok is False
    assert "negative" in (out.reason or "")


def test_check_refuses_over_per_action_cap():
    out = check_within_caps(
        amount_usdc=HARD_CAPS.per_action_usdc + 0.01,
        spent_today_usdc=0.0,
        spent_lifetime_usdc=0.0,
    )
    assert out.ok is False
    assert out.ceiling == "per_action"
    assert out.limit_usdc == HARD_CAPS.per_action_usdc


def test_check_refuses_when_day_total_would_exceed_cap():
    """An action that fits per-action but bumps today's total over the daily
    ceiling must be refused on per_day, not per_action."""
    out = check_within_caps(
        amount_usdc=10.0,
        spent_today_usdc=HARD_CAPS.per_day_usdc - 5.0,
        spent_lifetime_usdc=0.0,
    )
    assert out.ok is False
    assert out.ceiling == "per_day"
    assert out.limit_usdc == HARD_CAPS.per_day_usdc


def test_check_refuses_when_lifetime_total_would_exceed_cap():
    out = check_within_caps(
        amount_usdc=10.0,
        spent_today_usdc=0.0,
        spent_lifetime_usdc=HARD_CAPS.per_lifetime_usdc - 5.0,
    )
    assert out.ok is False
    assert out.ceiling == "per_lifetime"
    assert out.limit_usdc == HARD_CAPS.per_lifetime_usdc


def test_check_reports_tightest_ceiling_first():
    """When an action breaches multiple ceilings, the most specific one
    (per_action) wins so the operator sees the most actionable reason."""
    out = check_within_caps(
        amount_usdc=HARD_CAPS.per_action_usdc + 1.0,
        spent_today_usdc=HARD_CAPS.per_day_usdc,  # also over daily
        spent_lifetime_usdc=HARD_CAPS.per_lifetime_usdc,  # also over lifetime
    )
    assert out.ok is False
    assert out.ceiling == "per_action"


def test_check_respects_soft_cap_tightening():
    soft = OwnerSoftCaps(per_action_usdc=2.0)
    out = check_within_caps(
        amount_usdc=3.0,
        spent_today_usdc=0.0,
        spent_lifetime_usdc=0.0,
        soft=soft,
    )
    assert out.ok is False
    assert out.ceiling == "per_action"
    assert out.limit_usdc == 2.0


# -- HardCaps validator -------------------------------------------------------


def test_hard_caps_refuse_zero_or_negative_per_action():
    with pytest.raises(Exception):
        HardCaps(per_action_usdc=0.0, per_day_usdc=10.0, per_lifetime_usdc=100.0)


def test_hard_caps_refuse_out_of_order():
    """An action ceiling above the daily ceiling is a constitutional bug — the
    constructor must refuse rather than ship a silently broken cap set."""
    with pytest.raises(Exception):
        HardCaps(per_action_usdc=100.0, per_day_usdc=10.0, per_lifetime_usdc=1_000.0)


def test_cap_check_is_frozen():
    """The result must not be mutable after construction."""
    out = CapCheck(ok=True)
    with pytest.raises(Exception):
        out.ok = False  # type: ignore[misc]
