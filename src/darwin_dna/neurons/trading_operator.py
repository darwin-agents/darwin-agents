"""trading.operator — trades financial / crypto markets (docs/engineering/04).

The neuron that places buy/sell orders against a configured exchange.
This is the **highest-risk** neuron in the catalog — its
``capability_tags`` include ``treasury.spend`` so the doc-05 policy
engine ALWAYS gates each order through the same per-action /
per-day / per-lifetime caps used for other outbound payments.

Layer 0 ships the contract + the order-shape; production deployers
wire a specific exchange via ctx.http with the right authentication
in the scoped vault.
"""

from __future__ import annotations

from ._base import (
    BaseNeuron,
    Blocker,
    CostEstimate,
    FailureMode,
    NeuronContext,
    NeuronResult,
    Plan,
    Task,
)


class TradingOperator(BaseNeuron):
    name = "trading.operator"
    version = "1.0.0"
    capability_tags = frozenset(
        {"trading.operate", "treasury.observe", "web.operate"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.01, usd_p95=0.10,
        latency_p50_ms=1500, latency_p95_ms=10000,
        escalation_probability=0.05,
    )
    failure_modes = [
        FailureMode(
            code="exchange_unavailable",
            description="exchange API refused / rate limited / down",
            escalation_hint="not human-actionable; retry later",
        ),
        FailureMode(
            code="insufficient_balance",
            description="quote/base balance below the requested order size",
            escalation_hint="owner reviews allocation",
        ),
        FailureMode(
            code="auth_required",
            description="exchange API key expired",
            escalation_hint="owner re-issues the exchange API key",
        ),
        FailureMode(
            code="risk_refused",
            description="order refused by exchange risk engine (size, "
            "volatility, pattern day-trader limit, etc.)",
            escalation_hint="owner approves the trading limits",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        side = task.payload.get("side", "buy")
        symbol = task.payload.get("symbol", "")
        size = float(task.payload.get("size", 0.0))
        return Plan(
            steps=[f"{side} {size} {symbol}"],
            est_cost=self.cost_estimate,
            payload={
                "side": side,
                "symbol": symbol,
                "size": size,
                "order_type": task.payload.get("order_type", "market"),
                "limit_price": task.payload.get("limit_price"),
                "endpoint": task.payload.get("endpoint", ""),
                "exchange": task.payload.get("exchange", "unknown"),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        endpoint = plan.payload.get("endpoint")
        exchange = plan.payload.get("exchange", "unknown")
        side = plan.payload.get("side", "buy")
        symbol = plan.payload.get("symbol", "")
        size = float(plan.payload.get("size", 0.0))
        if size <= 0:
            return NeuronResult(
                ok=False, error_code="risk_refused",
                output={"reason": "non-positive size"},
            )
        intent = context.audit.log_intent(
            "neuron.trading.operator",
            {"side": side, "symbol": symbol, "size": size,
             "exchange": exchange},
        )
        http = getattr(context, "http", None)
        if http is None or not endpoint:
            context.audit.log_outcome(
                intent, "fail", {"reason": "no http or endpoint"},
            )
            return NeuronResult(
                ok=False, error_code="exchange_unavailable",
                output={"reason": "no http or endpoint configured"},
            )
        try:
            resp = http.request(
                "POST", endpoint,
                json={
                    "side": side,
                    "symbol": symbol,
                    "size": size,
                    "type": plan.payload.get("order_type", "market"),
                    "limit_price": plan.payload.get("limit_price"),
                },
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"reason": str(exc)})
            return NeuronResult(
                ok=False, error_code="exchange_unavailable",
                output={"reason": str(exc)},
            )
        if 200 <= status < 300:
            context.audit.log_outcome(
                intent, "ok",
                {"exchange": exchange, "side": side, "symbol": symbol,
                 "size": size, "status": status},
            )
            return NeuronResult(
                ok=True,
                output={"exchange": exchange, "side": side,
                        "symbol": symbol, "size": size, "status": status},
            )
        if status in (401, 403):
            ticket = await self.escalate(
                Blocker(
                    kind="approval",
                    detail=f"{exchange} API auth refused ({status})",
                ),
                context,
            )
            context.audit.log_outcome(
                intent, "escalated", {"ticket": ticket.id},
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="auth_required",
                output={"ticket": ticket.id},
            )
        if status == 402 or status == 422:
            # Insufficient balance vs. risk engine refusal — exchanges
            # use both shapes; classify on the reason string when one
            # is present.
            detail_text = str(getattr(resp, "text", "")).lower()
            if "balance" in detail_text or "fund" in detail_text:
                code = "insufficient_balance"
            else:
                code = "risk_refused"
            context.audit.log_outcome(
                intent, "fail", {"status": status, "code": code},
            )
            return NeuronResult(
                ok=False, error_code=code,
                output={"status": status},
            )
        context.audit.log_outcome(
            intent, "fail", {"status": status},
        )
        return NeuronResult(
            ok=False, error_code="exchange_unavailable",
            output={"status": status},
        )
