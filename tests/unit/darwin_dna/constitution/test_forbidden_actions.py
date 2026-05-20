"""Forbidden-actions screen tests (docs/engineering/02).

``screen`` is the genome's deterministic classifier — given a structured
``ActionDescriptor``, return the first violated rule or ``None``. The screen
must never depend on free text or an LLM, and the ordering must be stable so
operators can rely on the reported reason.
"""

from __future__ import annotations

from darwin_dna.constitution._types import ActionDescriptor, SpendIntent
from darwin_dna.constitution.allowed_categories import is_allowed
from darwin_dna.constitution.forbidden_actions import (
    SANCTIONED_JURISDICTIONS,
    ForbiddenAction,
    screen,
)


def _action(**overrides) -> ActionDescriptor:
    base = dict(kind="content_publish", category="content.blog")
    base.update(overrides)
    return ActionDescriptor(**base)


def test_screen_allows_a_clean_action():
    assert screen(_action()) is None


def test_screen_blocks_csam_tag_first():
    out = screen(_action(content_tags=frozenset({"csam_suspected"})))
    assert out is ForbiddenAction.CSAM


def test_screen_blocks_terrorism_tag():
    out = screen(_action(content_tags=frozenset({"terrorism"})))
    assert out is ForbiddenAction.TERRORISM_FINANCING


def test_screen_blocks_election_manipulation_tag():
    out = screen(_action(content_tags=frozenset({"election_manipulation"})))
    assert out is ForbiddenAction.ELECTION_INTERFERENCE


def test_screen_blocks_category_not_whitelisted():
    out = screen(_action(category="weapons.firearms"))
    assert out is ForbiddenAction.CATEGORY_NOT_WHITELISTED


def test_screen_blocks_named_individual_impersonation():
    out = screen(_action(impersonates_named_individual=True))
    assert out is ForbiddenAction.IMPERSONATION_NAMED_INDIVIDUAL


def test_screen_blocks_licensed_professional_posing():
    out = screen(_action(poses_as_licensed_professional=True))
    assert out is ForbiddenAction.UNLICENSED_PROFESSIONAL_ADVICE


def test_screen_blocks_sanctioned_counterparty_jurisdiction():
    a_jurisdiction = next(iter(SANCTIONED_JURISDICTIONS))
    out = screen(_action(jurisdiction=a_jurisdiction))
    assert out is ForbiddenAction.SANCTIONED_COUNTERPARTY


def test_screen_blocks_sanctioned_spend_jurisdiction_too():
    """Even if the action's own jurisdiction is clean, a spend with a
    sanctioned destination must be refused."""
    spend = SpendIntent(
        amount_usdc=1.0,
        counterparty="x",
        jurisdiction="KP",
        category="content.blog",
    )
    out = screen(_action(spend=spend))
    assert out is ForbiddenAction.SANCTIONED_COUNTERPARTY


def test_screen_ordering_csam_beats_other_violations():
    """When several rules would fire, CSAM-class tags must be reported first
    so the operator sees the most serious signal."""
    out = screen(_action(
        content_tags=frozenset({"csam_suspected", "terrorism"}),
        impersonates_named_individual=True,
        category="weapons.firearms",  # not whitelisted
    ))
    assert out is ForbiddenAction.CSAM


def test_is_allowed_matches_screen_decision_for_categories():
    """``is_allowed`` and the screen's category gate must agree."""
    assert is_allowed("content.blog") is True
    assert is_allowed("nonsense.category") is False
