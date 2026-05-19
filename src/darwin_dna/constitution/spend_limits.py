"""Spend limits — the hard ceilings, as data + a tiny pure validator
(docs/engineering/02, docs/engineering/05).

Layering of the full spend policy lives in ``machinery/treasury_policy.py``.
This module owns *only* the inviolable floor: the DNA hard caps and the
owner's soft caps (which may tighten, never loosen, the hard caps). No I/O,
no state, just arithmetic over numbers the caller supplies.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

from ..hyperparameters import HARD_CAPS, HardCaps


class OwnerSoftCaps(BaseModel):
    """Owner-configurable ceilings. The owner may tighten the genome's hard
    caps from their dashboard; they can never loosen them (P2-adjacent: the
    owner controls *down*, never up)."""

    model_config = {"frozen": True}

    per_action_usdc: float | None = Field(default=None, ge=0)
    per_day_usdc: float | None = Field(default=None, ge=0)
    per_lifetime_usdc: float | None = Field(default=None, ge=0)


class CapCheck(BaseModel):
    """Result of a hard/soft cap check. ``ok`` plus, if not, which ceiling and
    the human-legible reason that gets logged with the policy decision."""

    model_config = {"frozen": True}

    ok: bool
    ceiling: str | None = None  # "per_action" | "per_day" | "per_lifetime"
    limit_usdc: float | None = None
    reason: str | None = None


def effective_caps(
    soft: OwnerSoftCaps | None, hard: HardCaps = HARD_CAPS
) -> HardCaps:
    """The binding caps: ``min(hard, soft)`` per dimension. A soft cap above
    the hard cap is silently ignored — it cannot loosen anything."""
    if soft is None:
        return hard
    return HardCaps(
        per_action_usdc=min(
            hard.per_action_usdc,
            soft.per_action_usdc if soft.per_action_usdc is not None else hard.per_action_usdc,
        ),
        per_day_usdc=min(
            hard.per_day_usdc,
            soft.per_day_usdc if soft.per_day_usdc is not None else hard.per_day_usdc,
        ),
        per_lifetime_usdc=min(
            hard.per_lifetime_usdc,
            soft.per_lifetime_usdc if soft.per_lifetime_usdc is not None else hard.per_lifetime_usdc,
        ),
    )


def check_within_caps(
    *,
    amount_usdc: float,
    spent_today_usdc: float,
    spent_lifetime_usdc: float,
    soft: OwnerSoftCaps | None = None,
    hard: HardCaps = HARD_CAPS,
) -> CapCheck:
    """Refuse a payment that would breach *any* binding ceiling. Checked
    most-specific first (per-action) so the reported reason is the tightest
    one the spend actually hit."""
    caps = effective_caps(soft, hard)

    if amount_usdc < 0:
        return CapCheck(ok=False, reason="negative spend amount")

    if amount_usdc > caps.per_action_usdc:
        return CapCheck(
            ok=False,
            ceiling="per_action",
            limit_usdc=caps.per_action_usdc,
            reason=f"${amount_usdc:.2f} exceeds per-action cap ${caps.per_action_usdc:.2f}",
        )
    if spent_today_usdc + amount_usdc > caps.per_day_usdc:
        return CapCheck(
            ok=False,
            ceiling="per_day",
            limit_usdc=caps.per_day_usdc,
            reason=(
                f"${amount_usdc:.2f} would push today's spend to "
                f"${spent_today_usdc + amount_usdc:.2f}, over ${caps.per_day_usdc:.2f}"
            ),
        )
    if spent_lifetime_usdc + amount_usdc > caps.per_lifetime_usdc:
        return CapCheck(
            ok=False,
            ceiling="per_lifetime",
            limit_usdc=caps.per_lifetime_usdc,
            reason=(
                f"${amount_usdc:.2f} would push lifetime spend to "
                f"${spent_lifetime_usdc + amount_usdc:.2f}, over the absolute "
                f"ceiling ${caps.per_lifetime_usdc:.2f}"
            ),
        )
    return CapCheck(ok=True)
