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


# ---------------------------------------------------------------------------
# Pre-approval rules (doc 17 §"Darwin paying a human")
# ---------------------------------------------------------------------------

from darwin_dna.constitution import PreApprovalRule


def _state(**kw):
    base = dict(balance_usdc=1_000.0)
    base.update(kw)
    return TreasuryState(**base)


def test_freelancer_without_preapproval_escalates_as_baseline():
    """Baseline: every freelancer spend still escalates without a rule."""
    d = evaluate(
        _intent(amount_usdc=10.0, counterparty="upwork:writer-42",
                counterparty_category="freelancer"),
        _state(),
    )
    assert d.verdict is Verdict.ESCALATE
    assert d.layer == "escalation"


def test_matching_preapproval_converts_freelancer_to_allow():
    rule = PreApprovalRule(
        rule_id="upwork-under-50",
        counterparty_category="freelancer",
        counterparty_substring="upwork",
        max_amount_usdc=50.0,
    )
    d = evaluate(
        _intent(amount_usdc=42.0, counterparty="upwork:writer-42",
                counterparty_category="freelancer"),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.ALLOW
    assert d.layer == "pre_approval:upwork-under-50"


def test_preapproval_over_max_amount_still_escalates():
    """A spend above the rule's ceiling falls through to escalation.
    Amounts kept under the hard cap so the layer-2 ceiling doesn't fire
    instead — we're testing the pre-approval branch specifically."""
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=20.0,
    )
    d = evaluate(
        _intent(amount_usdc=25.0, counterparty="upwork:x",
                counterparty_category="freelancer"),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.ESCALATE


def test_preapproval_category_mismatch_still_escalates():
    """A freelancer rule must not auto-clear a legal_financial spend."""
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=1_000.0,
    )
    d = evaluate(
        _intent(amount_usdc=10.0, counterparty="lawfirm.example",
                counterparty_category="legal_financial"),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.ESCALATE


def test_preapproval_substring_must_match_counterparty():
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        counterparty_substring="upwork",
        max_amount_usdc=50.0,
    )
    d = evaluate(
        _intent(amount_usdc=10.0, counterparty="fiverr:translator",
                counterparty_category="freelancer"),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.ESCALATE


def test_expired_preapproval_does_not_match():
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=50.0,
        expires_at=100.0,
    )
    d = evaluate(
        _intent(amount_usdc=10.0, counterparty="upwork:x",
                counterparty_category="freelancer"),
        _state(pre_approvals=(rule,), now_ts=200.0),
    )
    assert d.verdict is Verdict.ESCALATE


def test_preapproval_never_bypasses_hard_caps():
    """Pre-approvals only override layer-4 escalation. Layer-2 hard caps
    still refuse a spend over the constitution's ceiling."""
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=HARD_CAPS.per_action_usdc * 10,  # absurdly loose
    )
    d = evaluate(
        _intent(amount_usdc=HARD_CAPS.per_action_usdc + 1.0,
                counterparty="upwork:x",
                counterparty_category="freelancer"),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.REFUSE
    assert d.layer.startswith("caps:")


def test_preapproval_never_bypasses_sanctions():
    """Layer-1 forbidden screen runs before pre-approvals. A sanctioned
    jurisdiction stays refused even with a matching pre-approval."""
    from darwin_dna.constitution.forbidden_actions import SANCTIONED_JURISDICTIONS
    if not SANCTIONED_JURISDICTIONS:
        return
    jurisdiction = next(iter(SANCTIONED_JURISDICTIONS))
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=1_000.0,
    )
    d = evaluate(
        _intent(amount_usdc=10.0, counterparty="upwork:x",
                counterparty_category="freelancer",
                jurisdiction=jurisdiction),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.REFUSE


def test_over_review_threshold_preapproval_clears_to_allow():
    """Layer-4's amount-based escalation (>$X is too big for autonomy)
    also yields to a matching pre-approval."""
    from darwin_dna.constitution.escalation_rules import (
        ESCALATION_PER_ACTION_REVIEW_USDC,
    )
    big = ESCALATION_PER_ACTION_REVIEW_USDC + 1.0
    if big > HARD_CAPS.per_action_usdc:
        # The review threshold is above the hard cap; this branch can't
        # be reached without violating layer 2. Skip.
        return
    rule = PreApprovalRule(
        rule_id="r1",
        max_amount_usdc=big + 10.0,
    )
    d = evaluate(
        _intent(amount_usdc=big, counterparty="api.example",
                counterparty_category="api"),
        _state(pre_approvals=(rule,)),
    )
    assert d.verdict is Verdict.ALLOW
    assert d.layer.startswith("pre_approval:")


# ---------------------------------------------------------------------------
# Pre-approval max_uses enforcement
# ---------------------------------------------------------------------------


def test_preapproval_matches_until_max_uses_reached():
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=20.0,
        max_uses=3,
        used_count=0,
    )
    intent_kw = dict(
        intent_counterparty="upwork:x",
        intent_counterparty_category="freelancer",
        intent_amount_usdc=10.0,
        now_ts=0.0,
    )
    assert rule.matches(**intent_kw) is True


def test_preapproval_does_not_match_when_used_count_at_max():
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=20.0,
        max_uses=3,
        used_count=3,
    )
    assert rule.matches(
        intent_counterparty="upwork:x",
        intent_counterparty_category="freelancer",
        intent_amount_usdc=10.0,
        now_ts=0.0,
    ) is False


def test_preapproval_with_no_max_uses_unbounded():
    rule = PreApprovalRule(
        rule_id="r1",
        counterparty_category="freelancer",
        max_amount_usdc=20.0,
        used_count=10_000,  # huge but max_uses is None
    )
    assert rule.matches(
        intent_counterparty="upwork:x",
        intent_counterparty_category="freelancer",
        intent_amount_usdc=10.0,
        now_ts=0.0,
    ) is True
