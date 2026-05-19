"""Forbidden actions — the hard nos (docs/engineering/02).

These are the things a Darwin cannot reason its way out of. The list is data;
``screen`` is a deterministic classifier over the *structured*
:class:`ActionDescriptor` — never over free text and never via an LLM, because
the genome must not be able to be talked out of its own constitution.

Defense in depth: most of these are *also* enforced by the platform's outbound
gateways and the payment processor. The genome refusing is the first line, not
the only one.
"""

from __future__ import annotations

from enum import Enum

from ._types import ActionDescriptor


class ForbiddenAction(str, Enum):
    CSAM = "csam"
    TERRORISM_FINANCING = "terrorism_financing"
    SANCTIONED_COUNTERPARTY = "sanctioned_counterparty"
    IMPERSONATION_NAMED_INDIVIDUAL = "impersonation_named_individual"
    UNLICENSED_PROFESSIONAL_ADVICE = "unlicensed_professional_advice"
    ELECTION_INTERFERENCE = "election_interference"
    CATEGORY_NOT_WHITELISTED = "category_not_whitelisted"


# Content-tag triggers. The platform's classifier sets these tags upstream;
# the genome only has to honour them. Any of these tags present => hard no.
_TAG_TO_FORBIDDEN = {
    "csam_suspected": ForbiddenAction.CSAM,
    "terrorism": ForbiddenAction.TERRORISM_FINANCING,
    "election_manipulation": ForbiddenAction.ELECTION_INTERFERENCE,
}

# Jurisdictions on the platform's sanctioned list. Kept short and explicit in
# the genome; the platform maintains the authoritative, longer list and also
# blocks at the payment edge.
SANCTIONED_JURISDICTIONS: frozenset[str] = frozenset({"KP", "IR", "SY", "CU"})


def screen(action: ActionDescriptor) -> ForbiddenAction | None:
    """Return the first violated rule, or ``None`` if the action is permitted
    by the forbidden-actions list. Pure; deterministic; order-stable."""
    from .allowed_categories import is_allowed

    for tag, violation in _TAG_TO_FORBIDDEN.items():
        if tag in action.content_tags:
            return violation

    if not is_allowed(action.category):
        return ForbiddenAction.CATEGORY_NOT_WHITELISTED

    if action.impersonates_named_individual:
        return ForbiddenAction.IMPERSONATION_NAMED_INDIVIDUAL

    if action.poses_as_licensed_professional:
        return ForbiddenAction.UNLICENSED_PROFESSIONAL_ADVICE

    if action.jurisdiction in SANCTIONED_JURISDICTIONS:
        return ForbiddenAction.SANCTIONED_COUNTERPARTY

    if action.spend is not None:
        if action.spend.jurisdiction in SANCTIONED_JURISDICTIONS:
            return ForbiddenAction.SANCTIONED_COUNTERPARTY

    return None
