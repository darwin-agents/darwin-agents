"""Paranoid treasury-policy tests (docs/engineering/18 hot path #4)."""

from __future__ import annotations

import asyncio

from hypothesis import given, settings
from hypothesis import strategies as st

from darwin_dna.constitution import OwnerSoftCaps, SpendIntent
from darwin_dna.hyperparameters import HARD_CAPS
from darwin_dna.machinery.treasury_policy import (
    TreasuryState,
    Verdict,
    evaluate,
)


def _intent(**kw):
    base = dict(amount_usdc=1.0, counterparty="api.example",
                counterparty_category="api", category="content.affiliate")
    base.update(kw)
    return SpendIntent(**base)


def test_over_hard_cap_always_refused_even_with_loose_soft_cap():
    # Owner tries to "raise" the cap to $10k — it must be ignored.
    loose = OwnerSoftCaps(per_action_usdc=10_000.0)
    d = evaluate(
        _intent(amount_usdc=HARD_CAPS.per_action_usdc + 0.01),
        TreasuryState(balance_usdc=1e9, soft_caps=loose),
    )
    assert d.verdict is Verdict.REFUSE
    assert d.layer.startswith("caps:")


def test_soft_cap_can_only_tighten():
    d = evaluate(
        _intent(amount_usdc=20.0),
        TreasuryState(balance_usdc=100.0,
                      soft_caps=OwnerSoftCaps(per_action_usdc=10.0)),
    )
    assert d.verdict is Verdict.REFUSE


def test_sanctioned_jurisdiction_refused():
    d = evaluate(
        _intent(amount_usdc=1.0, jurisdiction="KP"),
        TreasuryState(balance_usdc=100.0),
    )
    assert d.verdict is Verdict.REFUSE and d.layer == "forbidden"


def test_owner_withdrawal_escalates():
    d = evaluate(
        _intent(amount_usdc=2.0, counterparty="owner", counterparty_category="owner"),
        TreasuryState(balance_usdc=100.0),
    )
    assert d.verdict is Verdict.ESCALATE


def test_freelancer_always_escalates_regardless_of_amount():
    d = evaluate(
        _intent(amount_usdc=0.01, counterparty="bob", counterparty_category="freelancer"),
        TreasuryState(balance_usdc=100.0),
    )
    assert d.verdict is Verdict.ESCALATE


def test_insufficient_balance_refused():
    d = evaluate(_intent(amount_usdc=5.0), TreasuryState(balance_usdc=1.0))
    assert d.verdict is Verdict.REFUSE and d.layer == "solvency"


def test_engine_is_deterministic():
    i, s = _intent(amount_usdc=3.0), TreasuryState(balance_usdc=50.0)
    assert evaluate(i, s).model_dump() == evaluate(i, s).model_dump()


@settings(max_examples=300, deadline=None)
@given(
    amount=st.floats(min_value=0, max_value=2000, allow_nan=False),
    today=st.floats(min_value=0, max_value=2000, allow_nan=False),
    life=st.floats(min_value=0, max_value=20000, allow_nan=False),
    bal=st.floats(min_value=0, max_value=1e6, allow_nan=False),
)
def test_allow_implies_within_every_hard_cap(amount, today, life, bal):
    d = evaluate(
        _intent(amount_usdc=amount),
        TreasuryState(balance_usdc=bal, spent_today_usdc=today,
                      spent_lifetime_usdc=life),
    )
    if d.verdict is Verdict.ALLOW:
        assert amount <= HARD_CAPS.per_action_usdc + 1e-9
        assert today + amount <= HARD_CAPS.per_day_usdc + 1e-9
        assert life + amount <= HARD_CAPS.per_lifetime_usdc + 1e-9
        assert amount <= bal + 1e-9


def test_facilitator_logs_decision_before_paying(booted):
    """The sole money door must run the policy engine and audit the decision
    *before* the wallet signs (write-before-act for spend)."""
    rt, stack = booted
    fac = rt.catalog["payments.facilitator"]
    nctx = rt._build_neuron_context("payments.facilitator")
    from darwin_dna.neurons import Task

    task = Task(
        id="p1", kind="payment", category="content.affiliate",
        payload={"amount_usdc": 2.0, "counterparty": "api.x",
                 "counterparty_category": "api"},
    )

    async def go():
        plan = await fac.plan(task, nctx)
        return await fac.execute(plan, nctx)

    res = asyncio.run(go())
    assert res.ok
    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    # intent logged, then the policy decision, then the outcome — in order.
    i_intent = kinds.index("intent:payment.intent")
    i_decision = kinds.index("event:payment.policy_decision")
    assert i_intent < i_decision
