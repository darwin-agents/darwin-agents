"""The constitution — the things a Darwin cannot reason its way out of.

Data, not logic (mostly). Machinery imports from here; this package imports
nothing from machinery. The dependency arrow points one way.
"""

from ._principles import PRINCIPLES, ConstitutionError, assert_constitution_invariants
from ._types import (
    ActionDescriptor,
    ActionKind,
    CounterpartyCategory,
    SpendIntent,
)
from .allowed_categories import ALLOWED_CATEGORIES, is_allowed
from .escalation_rules import (
    ESCALATION_PER_ACTION_REVIEW_USDC,
    EscalationReason,
    TicketClass,
    classify_escalation,
    classify_ticket,
)
from .forbidden_actions import (
    SANCTIONED_JURISDICTIONS,
    ForbiddenAction,
    screen,
)
from .spend_limits import (
    CapCheck,
    OwnerSoftCaps,
    check_within_caps,
    effective_caps,
)

__all__ = [
    "PRINCIPLES",
    "ConstitutionError",
    "assert_constitution_invariants",
    "ActionDescriptor",
    "ActionKind",
    "CounterpartyCategory",
    "SpendIntent",
    "ALLOWED_CATEGORIES",
    "is_allowed",
    "EscalationReason",
    "TicketClass",
    "classify_escalation",
    "classify_ticket",
    "ESCALATION_PER_ACTION_REVIEW_USDC",
    "ForbiddenAction",
    "SANCTIONED_JURISDICTIONS",
    "screen",
    "CapCheck",
    "OwnerSoftCaps",
    "check_within_caps",
    "effective_caps",
]
