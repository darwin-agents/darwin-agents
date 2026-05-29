"""Owner pre-approval rules — a typed exception to layer-4 escalation.

The constitution's default for some counterparty categories (``freelancer``,
``legal_financial``, ``owner``) is to escalate every spend, no matter how
small, so a human signs off on each one (docs/engineering/05 §"escalation
rules"; docs/engineering/17 §"What about a Darwin paying a human?").

That's the right default for high-friction payment classes, but it makes
recurring small-ticket flows ("auto-approve Upwork escrow payments under
$50") onerous. A pre-approval rule is the owner's pre-committed answer:
*for the next 7 days, spends matching these criteria don't need to wake
me up — allow them automatically and log the rule that cleared them*.

Pre-approvals never *loosen* the constitution's hard rules:

- Hard caps (per-action / per-day / per-lifetime) and the policy engine's
  forbidden / sanctions / solvency / strategy-envelope / anomaly layers
  still apply unchanged. A pre-approval only skips the escalation step.
- The hard ceiling on `max_amount_usdc` per rule is also bounded by the
  DNA hard caps — a rule with `max_amount_usdc=100_000` against the
  per-action hard cap of $50 still refuses anything above $50.
- An expired rule is inert. Out-of-band revoke via the platform API
  takes effect on the next policy check.

The pre-approval consumption counter is not tracked here (the policy
engine is pure). The platform-side store may add a max-uses guard;
this model is the *match contract*, not the bookkeeping.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ._types import CounterpartyCategory


class PreApprovalRule(BaseModel):
    """One owner-pre-committed answer to an otherwise-escalating spend.

    The match predicate is conjunctive: every non-``None`` field must
    hold against the :class:`SpendIntent` for the rule to fire. A field
    set to ``None`` is "don't care".
    """

    model_config = {"frozen": True}

    rule_id: str  # owner-chosen or platform-minted; opaque to the engine

    # -- match criteria (all conjunctive when set) ------------------------
    counterparty_category: CounterpartyCategory | None = None
    """If set, the intent's ``counterparty_category`` must match exactly."""

    counterparty_substring: str | None = None
    """If set, must appear (case-insensitive) inside the intent's
    ``counterparty`` string. Substring rather than regex on purpose: an
    owner pre-approving payments has to be able to reason about what
    matches at a glance, and a regex is a foot-gun."""

    max_amount_usdc: float = Field(gt=0)
    """Per-spend ceiling for this rule. A spend at or below this amount
    matches; above it does not. Always upper-bounded by the DNA hard
    caps — the policy engine still checks those independently."""

    # -- lifetime ---------------------------------------------------------
    expires_at: float | None = None
    """Epoch-second deadline after which the rule is inert. ``None``
    means no expiry. Rules without an expiry should be rare — a
    standing pre-approval is closer to a category-wide change of policy
    than a convenience grant."""

    def matches(
        self,
        *,
        intent_counterparty: str,
        intent_counterparty_category: str,
        intent_amount_usdc: float,
        now_ts: float,
    ) -> bool:
        """Pure predicate: does this rule clear that intent at that time?

        Inputs are flat scalars rather than a :class:`SpendIntent` so
        callers can use the rule without importing the constitution's
        intent type — useful for the platform-side rule store admin UI."""
        if self.expires_at is not None and now_ts >= self.expires_at:
            return False
        if intent_amount_usdc > self.max_amount_usdc:
            return False
        if (
            self.counterparty_category is not None
            and intent_counterparty_category != self.counterparty_category
        ):
            return False
        if self.counterparty_substring is not None:
            needle = self.counterparty_substring.lower()
            if needle not in (intent_counterparty or "").lower():
                return False
        return True
