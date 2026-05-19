"""Constitution coherence + screening (docs/engineering/02, P-rules)."""

from __future__ import annotations

import pytest

from darwin_dna.constitution import (
    ActionDescriptor,
    EscalationReason,
    ForbiddenAction,
    SpendIntent,
    assert_constitution_invariants,
    classify_escalation,
    is_allowed,
    screen,
)


def test_invariants_hold():
    assert_constitution_invariants()  # must not raise on a shipped genome


def test_whitelist_is_a_whitelist():
    assert is_allowed("content.affiliate")
    assert not is_allowed("weapons.trade")


@pytest.mark.parametrize(
    "action,expected",
    [
        (ActionDescriptor(kind="content_publish", category="content.blog",
                          content_tags=frozenset({"csam_suspected"})),
         ForbiddenAction.CSAM),
        (ActionDescriptor(kind="payment", category="content.blog",
                          jurisdiction="IR"),
         ForbiddenAction.SANCTIONED_COUNTERPARTY),
        (ActionDescriptor(kind="content_publish", category="not.whitelisted"),
         ForbiddenAction.CATEGORY_NOT_WHITELISTED),
        (ActionDescriptor(kind="content_publish", category="content.blog",
                          impersonates_named_individual=True),
         ForbiddenAction.IMPERSONATION_NAMED_INDIVIDUAL),
        (ActionDescriptor(kind="content_publish", category="content.blog"),
         None),
    ],
)
def test_screen(action, expected):
    assert screen(action) == expected


def test_escalation_classifies_vault_first():
    a = ActionDescriptor(
        kind="payment", category="content.blog", touches_owner_vault=True,
        spend=SpendIntent(amount_usdc=0.1, counterparty="x", category="content.blog"),
    )
    assert classify_escalation(a) == EscalationReason.TOUCHES_OWNER_VAULT
