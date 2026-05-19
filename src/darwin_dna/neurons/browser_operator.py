"""browser.operator — the generic browser substrate (docs/engineering/04, 15).

Browser is a *substrate, not a domain*: domain work (publishing a post,
signing up for a service) drives this neuron when no stable API exists. It is
the captcha-hungry path, so it escalates rather than fails (P4).

Layer 0 has no real Browserbase; the fake substrate is injected via context.
The contract and the escalation surface are what matter at the genome level.
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


class BrowserOperator(BaseNeuron):
    name = "browser.operator"
    version = "1.0.0"
    capability_tags = frozenset({"web.operate", "web.publish", "web.signup"})
    cost_estimate = CostEstimate(
        usd_p50=0.05, usd_p95=0.30, latency_p50_ms=8000, latency_p95_ms=45000,
        escalation_probability=0.15,
    )
    failure_modes = [
        FailureMode(
            code="captcha",
            description="a human-verification wall",
            escalation_hint="owner solves captcha / approves the session",
        ),
        FailureMode(
            code="navigation_failed",
            description="page structure changed or site unreachable",
            escalation_hint="not human-actionable; retry, then cached-replay path",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        return Plan(
            steps=[f"open {task.payload.get('url', '<url>')}", "perform actions"],
            est_cost=self.cost_estimate,
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        intent = context.audit.log_intent("neuron.browser.operator", {"plan": plan.steps})
        substrate = getattr(context, "browser", None)
        if substrate is None:
            context.audit.log_outcome(intent, "fail", {"reason": "no substrate"})
            return NeuronResult(ok=False, error_code="navigation_failed")

        outcome = substrate.run(plan.steps)
        if outcome.get("blocked") == "captcha":
            ticket = await self.escalate(
                Blocker(kind="captcha", detail=outcome.get("detail", "captcha wall")),
                context,
            )
            context.audit.log_outcome(intent, "escalated", {"ticket": ticket.id})
            return NeuronResult(ok=False, escalated=True, error_code="captcha")

        if not outcome.get("ok"):
            context.audit.log_outcome(intent, "fail", {"reason": outcome.get("detail")})
            return NeuronResult(ok=False, error_code="navigation_failed")

        cost = float(outcome.get("cost_usdc", self.cost_estimate.usd_p50))
        context.audit.log_outcome(intent, "ok", {"cost_usdc": cost})
        return NeuronResult(ok=True, cost_actual_usdc=cost, output=outcome.get("data", {}))
