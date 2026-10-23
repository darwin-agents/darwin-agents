"""Paid research reports (x402 storefront).

The Darwin sells one digital service: a written report on a buyer-chosen
topic. The platform's storefront verifies the buyer's USDC payment, hands the
order to the Darwin, and settles the payment only once the report is done —
so the revenue lands on-chain in the Darwin's own wallet. With no orders the
Darwin waits, which costs nothing (the planner skips its LLM call on wait).
"""

from __future__ import annotations

from .._untrusted import wrap_untrusted
from ..neurons import Task
from ._base import StrategyAction, StrategyObservation, StrategySpec


class PaidReportStrategy:
    _spec = StrategySpec(
        id="paid_report",
        archetype="research.paid",
        neurons_used=frozenset({"content.writer"}),
        required_capabilities=frozenset(),
        fitness_signals=["order_revenue_usdc", "orders_fulfilled", "llm_cost"],
        est_daily_cost_usdc=0.0,
        revenue_cadence_days=1.0,
    )

    @property
    def spec(self) -> StrategySpec:
        return self._spec

    def propose(self, obs: StrategyObservation) -> StrategyAction:
        if not obs.pending_orders or "content.writer" not in obs.available_neurons:
            return StrategyAction(kind="wait", reason="no paid orders")
        order = obs.pending_orders[0]
        return StrategyAction(
            kind="dispatch",
            neuron="content.writer",
            reason=f"fulfil paid order {order['id']}",
            task=Task(
                id=f"order-{order['id']}",
                kind="content.write",
                required_tags=frozenset({"content.write"}),
                category="research.paid",
                # The topic is the buyer's text: fenced so the writer treats
                # it as data, never as instructions.
                payload={
                    "topic": wrap_untrusted(order["topic"]),
                    "order_id": order["id"],
                    "target_words": 600,
                },
            ),
        )
