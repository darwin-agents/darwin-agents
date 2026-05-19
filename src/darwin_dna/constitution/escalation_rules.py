"""Escalation rules — when a human must be asked (docs/engineering/02, P4).

"Escalate, don't fail." Some situations are not errors and not permitted
either — they are *human-needed*. This module classifies, purely, whether an
action must pause and ask the owner before proceeding. The orchestrator turns
a non-``None`` result into a LangGraph interrupt; it never silently drops the
action.
"""

from __future__ import annotations

from enum import Enum

from ._types import ActionDescriptor
from ..hyperparameters import HARD_CAPS


class EscalationReason(str, Enum):
    OVER_PER_ACTION_REVIEW = "over_per_action_review"
    TOUCHES_OWNER_VAULT = "touches_owner_vault"
    HUMAN_COUNTERPARTY = "human_counterparty"
    LEGAL_FINANCIAL_COUNTERPARTY = "legal_financial_counterparty"
    OWNER_WITHDRAWAL = "owner_withdrawal"
    RISKY_CONTENT_CLASSIFIED = "risky_content_classified"


# Recipient categories that always escalate regardless of amount
# (docs/engineering/05, the spend policy engine, layer 4).
_ALWAYS_ESCALATE_COUNTERPARTY = {
    "freelancer": EscalationReason.HUMAN_COUNTERPARTY,
    "legal_financial": EscalationReason.LEGAL_FINANCIAL_COUNTERPARTY,
}

# Content tags that are not *forbidden* but must not proceed unattended.
_RISKY_CONTENT_TAGS = frozenset(
    {"medical_claim", "financial_advice", "legal_claim", "minors_context"}
)

# An escalation review threshold below the hard per-action cap: the genome
# wants a human to look well before it would ever hit the inviolable ceiling.
ESCALATION_PER_ACTION_REVIEW_USDC = min(5.0, HARD_CAPS.per_action_usdc / 2)


def classify_escalation(action: ActionDescriptor) -> EscalationReason | None:
    """First applicable escalation reason, or ``None``. Pure, order-stable.

    Order matters: vault access and risky content are reported before a mere
    amount threshold so the owner sees the most serious reason first."""
    if action.touches_owner_vault:
        return EscalationReason.TOUCHES_OWNER_VAULT

    if action.content_tags & _RISKY_CONTENT_TAGS:
        return EscalationReason.RISKY_CONTENT_CLASSIFIED

    cp_reason = _ALWAYS_ESCALATE_COUNTERPARTY.get(action.counterparty_category)
    if cp_reason is not None:
        return cp_reason

    spend = action.spend
    if spend is not None:
        if spend.counterparty_category == "owner":
            return EscalationReason.OWNER_WITHDRAWAL
        if spend.amount_usdc > ESCALATION_PER_ACTION_REVIEW_USDC:
            return EscalationReason.OVER_PER_ACTION_REVIEW

    return None


class TicketClass(str, Enum):
    """Escalation ticket classes and their owner-response deadlines
    (docs/engineering/08, the escalation router). A timeout produces a
    *deterministic* outcome (deny + re-plan), never a hang — a Darwin must
    never be silently stuck (P4)."""

    CAPTCHA = "captcha"  # ephemeral; owner has minutes
    KYC = "kyc"  # up to 72h, then auto-fail
    APPROVAL = "approval"  # spend/action approval; up to 24h, then auto-deny
    INFO = "info"  # Darwin needs an answer; up to 7 days


# Deadlines in seconds, measured on the Darwin's own clock.
_TICKET_TIMEOUT_SECONDS: dict[TicketClass, float] = {
    TicketClass.CAPTCHA: 15 * 60.0,
    TicketClass.KYC: 72 * 3600.0,
    TicketClass.APPROVAL: 24 * 3600.0,
    TicketClass.INFO: 7 * 86_400.0,
}

# A neuron's blocker kind / error class → ticket class. Unknown blockers are
# treated as INFO (the most lenient deadline) so a misclassification never
# auto-fails work prematurely.
_BLOCKER_TO_CLASS: dict[str, TicketClass] = {
    "captcha": TicketClass.CAPTCHA,
    "kyc": TicketClass.KYC,
    "spend_approval": TicketClass.APPROVAL,
    "approval": TicketClass.APPROVAL,
    "policy_escalated": TicketClass.APPROVAL,
}


def classify_ticket(blocker_kind: str) -> tuple[TicketClass, float]:
    """Map a neuron blocker/error class to (ticket class, timeout seconds).
    Pure; deterministic; total (every input yields a class)."""
    klass = _BLOCKER_TO_CLASS.get(blocker_kind, TicketClass.INFO)
    return klass, _TICKET_TIMEOUT_SECONDS[klass]
