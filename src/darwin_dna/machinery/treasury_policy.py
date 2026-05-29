"""The wallet bouncer (docs/engineering/18 hot path #4, docs/engineering/05).

Every outbound payment passes through this engine before the wallet SDK can
sign anything. It is **pure**: a function of (intent, state). No I/O, no clock,
no randomness — so it is exhaustively testable and an adversarial soma has no
seam to widen.

The layers, applied in order; the first that fires decides:

1. Forbidden screen (sanctioned counterparty/jurisdiction) — REFUSE.
2. DNA hard caps ∧ owner soft caps — REFUSE.
3. Strategy budget envelope — REFUSE (the strategy may not exceed what it
   declared).
4. Escalation rules (recipient category, amount review, owner withdrawal) —
   ESCALATE.
5. Anomaly detection (spend pattern break) — ESCALATE.

Only the ``payments.facilitator`` neuron may call this, and it is the only
path to the wallet, so policy is enforced in exactly one place.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from ..constitution import (
    EscalationReason,
    ForbiddenAction,
    OwnerSoftCaps,
    PreApprovalRule,
    SpendIntent,
    check_within_caps,
)
from ..constitution.forbidden_actions import SANCTIONED_JURISDICTIONS
from ..constitution.escalation_rules import ESCALATION_PER_ACTION_REVIEW_USDC
from ..hyperparameters import HARD_CAPS, HardCaps


class Verdict(str, Enum):
    ALLOW = "allow"
    REFUSE = "refuse"  # not permitted; the spend dies here
    ESCALATE = "escalate"  # pause and ask the owner (P4)


class StrategyEnvelope(BaseModel):
    """A soma-declared "I want to spend up to $X on this experiment". The
    engine enforces it; the strategy cannot quietly exceed it."""

    model_config = {"frozen": True}

    strategy_id: str
    cap_usdc: float = Field(ge=0)
    spent_usdc: float = Field(ge=0)


class TreasuryState(BaseModel):
    """Everything the engine needs, supplied by the caller. Note there is no
    wallet handle here — the engine never moves money, it only judges."""

    model_config = {"frozen": True}

    balance_usdc: float
    spent_today_usdc: float = 0.0
    spent_lifetime_usdc: float = 0.0
    soft_caps: OwnerSoftCaps | None = None
    envelope: StrategyEnvelope | None = None
    # Recent successful spend amounts, for anomaly detection. Cheap, bounded.
    recent_spend_usdc: tuple[float, ...] = ()
    # Owner pre-approval rules — typed exceptions to layer-4 escalation.
    # An empty tuple keeps the legacy behaviour (every freelancer /
    # legal_financial / owner / over-threshold spend escalates).
    pre_approvals: tuple[PreApprovalRule, ...] = ()
    # Wall-clock seconds for pre-approval expiry checks. The engine itself
    # stays pure — the caller supplies the time. Default 0.0 means "no
    # rule has expired" relative to any non-None expires_at the owner set,
    # which keeps legacy callers (no pre_approvals) unaffected.
    now_ts: float = 0.0


class PolicyDecision(BaseModel):
    """The verdict plus the rationale that gets logged *before* the payment
    is allowed to proceed (docs/engineering/18: decision logged with its
    rationale)."""

    model_config = {"frozen": True}

    verdict: Verdict
    reason: str
    layer: str  # which layer decided — for replayable "why" in the dashboard
    escalation_reason: EscalationReason | None = None
    forbidden: ForbiddenAction | None = None


# Anomaly thresholds. Conservative and explicit — anomaly detection that is
# itself surprising is worse than none.
_ANOMALY_MIN_HISTORY = 5  # need a baseline before we can call something anomalous
_ANOMALY_MULTIPLE = 20.0  # 20x the recent max
_ANOMALY_FLOOR_USDC = 1.0  # ignore noise below a dollar


def evaluate(
    intent: SpendIntent,
    state: TreasuryState,
    hard: HardCaps = HARD_CAPS,
) -> PolicyDecision:
    """The whole engine. Pure. Deterministic. Order-stable."""

    # Layer 1 — forbidden counterparty / jurisdiction. The genome refuses
    # outright; the platform's payment edge refuses too (defense in depth).
    if intent.jurisdiction in SANCTIONED_JURISDICTIONS:
        return PolicyDecision(
            verdict=Verdict.REFUSE,
            layer="forbidden",
            forbidden=ForbiddenAction.SANCTIONED_COUNTERPARTY,
            reason=f"counterparty jurisdiction {intent.jurisdiction} is sanctioned",
        )

    # Layer 2 — DNA hard caps ∧ owner soft caps. Inviolable. Worst-case
    # forward cost (p95) is checked too: a plan whose tail could blow the cap
    # is refused now, not after it has half-spent.
    probe = max(intent.amount_usdc, intent.est_p95_usdc or 0.0)
    cap = check_within_caps(
        amount_usdc=probe,
        spent_today_usdc=state.spent_today_usdc,
        spent_lifetime_usdc=state.spent_lifetime_usdc,
        soft=state.soft_caps,
        hard=hard,
    )
    if not cap.ok:
        return PolicyDecision(
            verdict=Verdict.REFUSE,
            layer=f"caps:{cap.ceiling or 'invalid'}",
            reason=cap.reason or "spend cap exceeded",
        )

    # Spending money you do not have is not a policy question, but the engine
    # is the last sane place to catch it before the wallet reverts.
    if intent.amount_usdc > state.balance_usdc:
        return PolicyDecision(
            verdict=Verdict.REFUSE,
            layer="solvency",
            reason=(
                f"insufficient balance: ${intent.amount_usdc:.2f} requested, "
                f"${state.balance_usdc:.2f} available"
            ),
        )

    # Layer 3 — strategy envelope. The soma promised a ceiling for this
    # experiment; hold it to that.
    env = state.envelope
    if env is not None and intent.strategy_id == env.strategy_id:
        if env.spent_usdc + intent.amount_usdc > env.cap_usdc:
            return PolicyDecision(
                verdict=Verdict.REFUSE,
                layer="strategy_envelope",
                reason=(
                    f"strategy {env.strategy_id} would spend "
                    f"${env.spent_usdc + intent.amount_usdc:.2f}, over its "
                    f"declared ${env.cap_usdc:.2f} envelope"
                ),
            )

    # Layer 4 — escalation rules. Permitted, but a human must say yes first.
    #
    # Pre-approvals are checked BEFORE we decide to escalate: an owner-set
    # rule that matches this intent converts what would have been an
    # ESCALATE into an ALLOW with the rule_id recorded in the decision
    # layer. The rule may not exceed the constitution's hard caps — those
    # already passed Layer 2 above. Solvency, anomaly, sanctions, and
    # strategy envelope are unaffected; pre-approvals only suppress the
    # human-review step for matching low-friction flows the owner
    # explicitly opted into.
    def _matching_rule() -> PreApprovalRule | None:
        for rule in state.pre_approvals:
            if rule.matches(
                intent_counterparty=intent.counterparty,
                intent_counterparty_category=intent.counterparty_category,
                intent_amount_usdc=intent.amount_usdc,
                now_ts=state.now_ts,
            ):
                return rule
        return None

    if intent.counterparty_category == "owner":
        rule = _matching_rule()
        if rule is not None:
            return PolicyDecision(
                verdict=Verdict.ALLOW,
                layer=f"pre_approval:{rule.rule_id}",
                reason=(
                    f"pre-approved by rule {rule.rule_id} "
                    f"(owner withdrawal, ≤${rule.max_amount_usdc:.2f})"
                ),
            )
        return PolicyDecision(
            verdict=Verdict.ESCALATE,
            layer="escalation",
            escalation_reason=EscalationReason.OWNER_WITHDRAWAL,
            reason="owner withdrawal/harvest requires owner confirmation",
        )
    if intent.counterparty_category == "freelancer":
        rule = _matching_rule()
        if rule is not None:
            return PolicyDecision(
                verdict=Verdict.ALLOW,
                layer=f"pre_approval:{rule.rule_id}",
                reason=(
                    f"pre-approved by rule {rule.rule_id} "
                    f"(freelancer, ≤${rule.max_amount_usdc:.2f})"
                ),
            )
        return PolicyDecision(
            verdict=Verdict.ESCALATE,
            layer="escalation",
            escalation_reason=EscalationReason.HUMAN_COUNTERPARTY,
            reason="paying a human freelancer always escalates",
        )
    if intent.counterparty_category == "legal_financial":
        rule = _matching_rule()
        if rule is not None:
            return PolicyDecision(
                verdict=Verdict.ALLOW,
                layer=f"pre_approval:{rule.rule_id}",
                reason=(
                    f"pre-approved by rule {rule.rule_id} "
                    f"(legal/financial, ≤${rule.max_amount_usdc:.2f})"
                ),
            )
        return PolicyDecision(
            verdict=Verdict.ESCALATE,
            layer="escalation",
            escalation_reason=EscalationReason.LEGAL_FINANCIAL_COUNTERPARTY,
            reason="legal/financial counterparty always escalates",
        )
    if intent.amount_usdc > ESCALATION_PER_ACTION_REVIEW_USDC:
        rule = _matching_rule()
        if rule is not None:
            return PolicyDecision(
                verdict=Verdict.ALLOW,
                layer=f"pre_approval:{rule.rule_id}",
                reason=(
                    f"pre-approved by rule {rule.rule_id} "
                    f"(over-review-threshold, ≤${rule.max_amount_usdc:.2f})"
                ),
            )
        return PolicyDecision(
            verdict=Verdict.ESCALATE,
            layer="escalation",
            escalation_reason=EscalationReason.OVER_PER_ACTION_REVIEW,
            reason=(
                f"${intent.amount_usdc:.2f} is over the "
                f"${ESCALATION_PER_ACTION_REVIEW_USDC:.2f} human-review threshold"
            ),
        )

    # Layer 5 — anomaly detection. A sudden break from the established spend
    # pattern pauses and escalates rather than refusing: it might be fine,
    # but a human should see it.
    hist = state.recent_spend_usdc
    if len(hist) >= _ANOMALY_MIN_HISTORY:
        baseline = max(hist)
        if (
            intent.amount_usdc > _ANOMALY_FLOOR_USDC
            and intent.amount_usdc > max(baseline, _ANOMALY_FLOOR_USDC) * _ANOMALY_MULTIPLE
        ):
            return PolicyDecision(
                verdict=Verdict.ESCALATE,
                layer="anomaly",
                escalation_reason=EscalationReason.OVER_PER_ACTION_REVIEW,
                reason=(
                    f"${intent.amount_usdc:.2f} is >{_ANOMALY_MULTIPLE:.0f}x the "
                    f"recent max ${baseline:.2f}: spend-pattern anomaly"
                ),
            )

    return PolicyDecision(
        verdict=Verdict.ALLOW,
        layer="clear",
        reason=f"within all caps and policy (${intent.amount_usdc:.2f})",
    )
