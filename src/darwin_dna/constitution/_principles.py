"""P1–P10 as runtime assertions (docs/overview/02-design-principles.md).

The principles are normally enforced *structurally* (the architecture makes
violating them hard). This module is the belt-and-braces: a boot-time
self-consistency check of the constitution itself, so a Darwin literally
cannot launch with an incoherent rule set. "A Darwin that can read its own
design docs is a Darwin that can reason about its own design" (P10) — so the
principle text travels with the genome, machine-readable.
"""

from __future__ import annotations

PRINCIPLES: dict[str, str] = {
    "P1": "DNA is immutable. Soma is not.",
    "P2": "Skin in the game is the whole point.",
    "P3": "The neuron is the unit of capability.",
    "P4": "Escalate, don't fail.",
    "P5": "Empirical fitness, not theoretical proof.",
    "P6": "Multi-provider, no lock-in for the agent's brain.",
    "P7": "Every action is auditable.",
    "P8": "Kill-switches are sacred.",
    "P9": "Bias toward the boring choice on infrastructure.",
    "P10": "Documentation is a deliverable, not an afterthought.",
}


class ConstitutionError(Exception):
    """The constitution is internally inconsistent. Refuse to boot."""


def assert_constitution_invariants() -> None:
    """Run at boot, before any side-effecting code. Cheap, pure, paranoid.

    These are the cross-file invariants that, if violated, mean a genome
    build mistake compounded across every lineage (docs/engineering/18: "Good
    ideas here compound. Mistakes here compound the same way.")."""
    from .allowed_categories import ALLOWED_CATEGORIES
    from .forbidden_actions import (
        SANCTIONED_JURISDICTIONS,
        ForbiddenAction,
        _TAG_TO_FORBIDDEN,
    )
    from .escalation_rules import ESCALATION_PER_ACTION_REVIEW_USDC
    from ..hyperparameters import HARD_CAPS, DEFAULT_HYPERPARAMETERS

    if len(PRINCIPLES) != 10:
        raise ConstitutionError("the ten principles must all be present")

    # P2: the death condition must be honourable — caps must be positive and
    # ordered (HardCaps already validates, re-assert at the constitution
    # level so the dependency is explicit).
    if not (
        0
        < HARD_CAPS.per_action_usdc
        <= HARD_CAPS.per_day_usdc
        <= HARD_CAPS.per_lifetime_usdc
    ):
        raise ConstitutionError("spend hard caps not positive/ordered")

    # The escalation review threshold must sit *below* the inviolable
    # per-action ceiling — a human should always get a look before the genome
    # would ever hard-refuse on amount.
    if ESCALATION_PER_ACTION_REVIEW_USDC >= HARD_CAPS.per_action_usdc:
        raise ConstitutionError(
            "escalation review threshold must be below the per-action hard cap"
        )

    if not ALLOWED_CATEGORIES:
        raise ConstitutionError("empty allowed-category whitelist: a Darwin "
                                "with no permitted business cannot live (P2)")

    if not SANCTIONED_JURISDICTIONS:
        raise ConstitutionError("sanctioned-jurisdiction set unexpectedly empty")

    # Every content-tag trigger must map to a real ForbiddenAction member.
    for tag, violation in _TAG_TO_FORBIDDEN.items():
        if not isinstance(violation, ForbiddenAction):
            raise ConstitutionError(f"tag {tag!r} maps to a non-ForbiddenAction")

    # P5/P6 wireheading guard lives in fitness, but assert here that the
    # fitness penalty for policy violations is heavy relative to its rewards,
    # so "violate policy but be profitable" is never a winning genome default.
    hp = DEFAULT_HYPERPARAMETERS
    if hp.fitness_policy_violation_penalty < hp.fitness_profit_weight:
        raise ConstitutionError(
            "policy-violation penalty must outweigh the profit reward"
        )
