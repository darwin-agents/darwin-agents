"""Escalation-rule constitution tests (docs/engineering/02 §P4).

A Darwin must escalate to its owner — not fail — when an action enters a
human-needed zone (vault touch, risky content, legal counterparty, owner
withdrawal, or an amount above the review threshold). This file pins the
ordering of those checks and the ticket-class → timeout mapping.
"""

from __future__ import annotations

from darwin_dna.constitution._types import ActionDescriptor, SpendIntent
from darwin_dna.constitution.escalation_rules import (
    ESCALATION_PER_ACTION_REVIEW_USDC,
    EscalationReason,
    TicketClass,
    classify_escalation,
    classify_ticket,
    ticket_timeout_seconds,
)


def _action(**overrides) -> ActionDescriptor:
    base = dict(kind="content_publish", category="content.blog")
    base.update(overrides)
    return ActionDescriptor(**base)


# -- classify_escalation ------------------------------------------------------


def test_no_escalation_for_a_clean_action():
    assert classify_escalation(_action()) is None


def test_vault_touch_escalates_first():
    """Vault access is the most serious escalation reason; even if other
    rules would also fire, this must be reported first."""
    out = classify_escalation(_action(
        touches_owner_vault=True,
        content_tags=frozenset({"medical_claim"}),
        counterparty_category="freelancer",
    ))
    assert out is EscalationReason.TOUCHES_OWNER_VAULT


def test_risky_content_tag_escalates_before_counterparty():
    out = classify_escalation(_action(
        content_tags=frozenset({"medical_claim"}),
        counterparty_category="freelancer",
    ))
    assert out is EscalationReason.RISKY_CONTENT_CLASSIFIED


def test_human_counterparty_escalates():
    out = classify_escalation(_action(counterparty_category="freelancer"))
    assert out is EscalationReason.HUMAN_COUNTERPARTY


def test_legal_financial_counterparty_escalates():
    out = classify_escalation(_action(counterparty_category="legal_financial"))
    assert out is EscalationReason.LEGAL_FINANCIAL_COUNTERPARTY


def test_owner_withdrawal_escalates():
    spend = SpendIntent(
        amount_usdc=0.5,
        counterparty="owner",
        counterparty_category="owner",
        category="content.blog",
    )
    out = classify_escalation(_action(kind="payment", spend=spend))
    assert out is EscalationReason.OWNER_WITHDRAWAL


def test_amount_above_review_threshold_escalates():
    spend = SpendIntent(
        amount_usdc=ESCALATION_PER_ACTION_REVIEW_USDC + 0.01,
        counterparty="vendor",
        counterparty_category="api",
        category="content.blog",
    )
    out = classify_escalation(_action(kind="payment", spend=spend))
    assert out is EscalationReason.OVER_PER_ACTION_REVIEW


def test_amount_at_or_below_review_threshold_does_not_escalate():
    spend = SpendIntent(
        amount_usdc=ESCALATION_PER_ACTION_REVIEW_USDC,
        counterparty="vendor",
        counterparty_category="api",
        category="content.blog",
    )
    assert classify_escalation(_action(kind="payment", spend=spend)) is None


# -- classify_ticket / timeouts ----------------------------------------------


def test_classify_ticket_maps_known_blockers_to_class_and_timeout():
    klass, timeout = classify_ticket("captcha")
    assert klass is TicketClass.CAPTCHA
    assert timeout == ticket_timeout_seconds(TicketClass.CAPTCHA)


def test_classify_ticket_unknown_falls_to_info_class_not_failure():
    """An unrecognised blocker must NOT auto-fail — it gets the most lenient
    deadline (INFO) so a misclassification never auto-denies real work."""
    klass, timeout = classify_ticket("never-heard-of-this")
    assert klass is TicketClass.INFO
    assert timeout == ticket_timeout_seconds(TicketClass.INFO)


def test_ticket_timeouts_are_strictly_ordered_by_severity():
    """Captcha is fastest, info is slowest — the platform relies on this
    ordering when surfacing countdowns."""
    captcha = ticket_timeout_seconds(TicketClass.CAPTCHA)
    approval = ticket_timeout_seconds(TicketClass.APPROVAL)
    kyc = ticket_timeout_seconds(TicketClass.KYC)
    info = ticket_timeout_seconds(TicketClass.INFO)
    assert captcha < approval < kyc <= info


def test_review_threshold_never_exceeds_half_hard_per_action_cap():
    """If the hard per-action cap ever shrinks, the review threshold must
    track it (it's defined as min(5, hard/2))."""
    from darwin_dna.hyperparameters import HARD_CAPS

    assert ESCALATION_PER_ACTION_REVIEW_USDC <= HARD_CAPS.per_action_usdc / 2
