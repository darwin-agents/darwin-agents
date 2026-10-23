"""Platform billing (docs/engineering/08 §"Billing model").

Two revenue streams the platform earns from a hosted Darwin:

1. **Platform fee on profit.** When the owner withdraws from a Darwin's
   treasury via the harvest path, the platform takes a configurable
   percentage (default 20%). Computed at harvest time, not on every
   revenue event — the user can leave money in the treasury indefinitely
   without being taxed.

2. **Infrastructure pass-through with margin.** Every metered debit the
   genome makes (LLM tokens, compute, sandbox hours — anything billed via
   ``wallet.debit(...)``) is the Darwin paying *the platform* for what
   the platform then pays to the underlying provider. A small markup
   makes that pass-through profitable; the platform's revenue is the
   markup portion of each metered debit.

The billing layer **does not run inside the genome**. The genome's wallet
records its own movements; the supervisor sweeps those at the heartbeat
boundary and attributes platform revenue. That isolation means a
misbehaving billing policy cannot corrupt the genome's audit chain — at
worst the per-Darwin platform-revenue figure is off, which the operator
sees in the snapshot and can re-derive from the raw wallet ledger.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class BillingPolicy:
    """Per-fleet billing knobs. Owners cannot raise these (they are platform
    parameters, set at deploy time)."""

    # Platform fee on the gross harvest amount. Doc 08's default is 20%.
    harvest_fee_rate: float = 0.20
    # Markup applied to the platform's underlying provider cost when billing
    # the Darwin's treasury. The Darwin sees the marked-up rate; the
    # platform's revenue per debit is ``amount * markup / (1 + markup)``.
    # 10% markup → ~9.09% of each metered debit is platform revenue.
    infrastructure_markup_rate: float = 0.10

    def harvest_fee(
        self, gross_usdc: float, profit_usdc: float | None = None,
    ) -> float:
        """Fee on a harvest of ``gross_usdc``. Returns a non-negative float
        capped at ``gross_usdc`` (no negative net for the owner). With
        ``profit_usdc`` the fee applies only to the profit part of the draw:
        an owner withdrawing their own principal pays nothing on it."""
        gross = max(0.0, float(gross_usdc))
        feeable = gross if profit_usdc is None else min(gross, max(0.0, float(profit_usdc)))
        return min(gross, feeable * self.harvest_fee_rate)

    def revenue_from_metered(self, amount_usdc: float) -> tuple[float, float]:
        """Split one metered Darwin-side debit into (provider_cost,
        platform_revenue). ``amount_usdc`` is what the Darwin paid; the
        platform's actual provider invoice is the smaller of the two."""
        amount = max(0.0, float(amount_usdc))
        if self.infrastructure_markup_rate <= 0:
            return amount, 0.0
        provider_cost = amount / (1.0 + self.infrastructure_markup_rate)
        return provider_cost, amount - provider_cost


@dataclass
class BillingLedger:
    """Per-Darwin accumulated billing figures. Append-only via
    ``add_metered`` / ``add_harvest``; ``summary`` is the JSON-friendly read
    view the API + snapshot emit."""

    darwin_id: str
    # Infrastructure pass-through.
    metered_gross_usdc: float = 0.0
    metered_provider_cost_usdc: float = 0.0
    metered_platform_revenue_usdc: float = 0.0
    metered_events: int = 0
    # Harvests (owner withdrawals).
    harvests_gross_usdc: float = 0.0
    harvests_net_usdc: float = 0.0
    harvests_fee_usdc: float = 0.0
    harvest_events: int = 0
    # Recent harvests as audit-friendly rows.
    harvest_history: list[dict[str, Any]] = field(default_factory=list)

    def add_metered(
        self, amount_usdc: float, policy: BillingPolicy, *, memo: str | None = None
    ) -> tuple[float, float]:
        provider, revenue = policy.revenue_from_metered(amount_usdc)
        self.metered_gross_usdc += float(amount_usdc)
        self.metered_provider_cost_usdc += provider
        self.metered_platform_revenue_usdc += revenue
        self.metered_events += 1
        return provider, revenue

    def add_harvest(
        self,
        *,
        gross_usdc: float,
        fee_usdc: float,
        destination: str,
        ts: float,
    ) -> dict[str, Any]:
        net = max(0.0, float(gross_usdc) - float(fee_usdc))
        self.harvests_gross_usdc += float(gross_usdc)
        self.harvests_net_usdc += net
        self.harvests_fee_usdc += float(fee_usdc)
        self.harvest_events += 1
        row = {
            "ts": float(ts),
            "gross_usdc": float(gross_usdc),
            "fee_usdc": float(fee_usdc),
            "net_usdc": net,
            "destination": destination,
        }
        self.harvest_history.append(row)
        # Bound the in-memory history — the wallet's own ledger is the
        # authoritative replayable record; we keep a recent slice for the
        # API. 256 entries ~ a year of weekly harvests.
        if len(self.harvest_history) > 256:
            del self.harvest_history[:-256]
        return row

    def summary(self) -> dict[str, Any]:
        return {
            "darwin_id": self.darwin_id,
            "infrastructure": {
                "gross_usdc": round(self.metered_gross_usdc, 6),
                "provider_cost_usdc": round(self.metered_provider_cost_usdc, 6),
                "platform_revenue_usdc": round(self.metered_platform_revenue_usdc, 6),
                "events": self.metered_events,
            },
            "harvests": {
                "gross_usdc": round(self.harvests_gross_usdc, 6),
                "net_usdc": round(self.harvests_net_usdc, 6),
                "fee_usdc": round(self.harvests_fee_usdc, 6),
                "events": self.harvest_events,
                "history_tail": [dict(h) for h in self.harvest_history[-10:]],
            },
            "total_platform_revenue_usdc": round(
                self.metered_platform_revenue_usdc + self.harvests_fee_usdc, 6
            ),
        }


class BillingService:
    """Fleet-wide billing state. The supervisor owns one of these and feeds
    it from each drive cycle (metered sweep) and from harvest calls."""

    def __init__(
        self,
        policy: BillingPolicy | None = None,
        *,
        clock=time.time,
    ) -> None:
        self._policy = policy or BillingPolicy()
        self._clock = clock
        self._ledgers: dict[str, BillingLedger] = {}

    @property
    def policy(self) -> BillingPolicy:
        return self._policy

    def for_darwin(self, darwin_id: str) -> BillingLedger:
        ledger = self._ledgers.get(darwin_id)
        if ledger is None:
            ledger = BillingLedger(darwin_id=darwin_id)
            self._ledgers[darwin_id] = ledger
        return ledger

    def record_metered(
        self, darwin_id: str, amount_usdc: float, *, memo: str | None = None
    ) -> tuple[float, float]:
        return self.for_darwin(darwin_id).add_metered(
            amount_usdc, self._policy, memo=memo
        )

    def record_harvest(
        self,
        darwin_id: str,
        *,
        gross_usdc: float,
        fee_usdc: float,
        destination: str,
    ) -> dict[str, Any]:
        return self.for_darwin(darwin_id).add_harvest(
            gross_usdc=gross_usdc,
            fee_usdc=fee_usdc,
            destination=destination,
            ts=self._clock(),
        )

    def summary(self, darwin_id: str) -> dict[str, Any]:
        return self.for_darwin(darwin_id).summary()

    def fleet_summary(self) -> dict[str, Any]:
        """Aggregated revenue across every Darwin the supervisor has hosted.
        Per-Darwin rows are sorted by total platform revenue desc so an
        operator's "who's making us money" sweep is one read."""
        rows = [l.summary() for l in self._ledgers.values()]
        rows.sort(
            key=lambda r: r["total_platform_revenue_usdc"], reverse=True
        )
        total = {
            "platform_revenue_usdc": round(
                sum(r["total_platform_revenue_usdc"] for r in rows), 6
            ),
            "metered_revenue_usdc": round(
                sum(r["infrastructure"]["platform_revenue_usdc"] for r in rows),
                6,
            ),
            "harvest_fees_usdc": round(
                sum(r["harvests"]["fee_usdc"] for r in rows), 6
            ),
            "darwins": len(rows),
        }
        return {"total": total, "per_darwin": rows,
                "policy": {
                    "harvest_fee_rate": self._policy.harvest_fee_rate,
                    "infrastructure_markup_rate":
                        self._policy.infrastructure_markup_rate,
                }}
