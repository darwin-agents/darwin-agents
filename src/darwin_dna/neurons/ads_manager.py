"""ads.manager — buys / optimizes paid acquisition (docs/engineering/04).

The neuron that talks to Meta / Google / X / LinkedIn ad APIs to
create, pause, and adjust campaigns. Talks through ctx.http for the
API call; the wallet bills via the existing x402 / metered-debit
path so spend on ads shows up in the same ledger the treasury policy
already gates.

Layer 0 ships the contract + the campaign-create / pause / budget-set
shape; the orchestrator + soma decide WHICH campaigns to run.
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


class AdsManager(BaseNeuron):
    name = "ads.manager"
    version = "1.0.0"
    capability_tags = frozenset(
        {"ads.manage", "web.operate", "treasury.observe"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.02, usd_p95=0.15,
        latency_p50_ms=2000, latency_p95_ms=15000,
        escalation_probability=0.02,
    )
    failure_modes = [
        FailureMode(
            code="ads_api_unavailable",
            description="ad platform API refused or rate-limited",
            escalation_hint="not human-actionable; retry with smaller budget",
        ),
        FailureMode(
            code="account_disabled",
            description="ad account suspended or restricted",
            escalation_hint="owner contacts the ad platform to reinstate",
        ),
        FailureMode(
            code="auth_required",
            description="ad API token expired",
            escalation_hint="owner refreshes the API token",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        action = task.payload.get("action", "create_campaign")
        platform = task.payload.get("platform", "meta")
        return Plan(
            steps=[f"{action} on {platform}"],
            est_cost=self.cost_estimate,
            payload={
                "action": action,
                "platform": platform,
                "endpoint": task.payload.get("endpoint", ""),
                "campaign": task.payload.get("campaign", {}),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        action = plan.payload.get("action", "create_campaign")
        platform = plan.payload.get("platform", "meta")
        endpoint = plan.payload.get("endpoint", "") or _DEFAULT_ENDPOINTS.get(
            platform, "",
        )
        intent = context.audit.log_intent(
            "neuron.ads.manager",
            {"action": action, "platform": platform},
        )

        http = getattr(context, "http", None)
        if http is None or not endpoint:
            context.audit.log_outcome(
                intent, "fail", {"reason": "no http or endpoint"},
            )
            return NeuronResult(
                ok=False, error_code="ads_api_unavailable",
                output={"reason": "no http transport or no endpoint"},
            )

        try:
            resp = http.request(
                "POST", endpoint,
                json={
                    "action": action,
                    "campaign": plan.payload.get("campaign", {}),
                },
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"reason": str(exc)})
            return NeuronResult(
                ok=False, error_code="ads_api_unavailable",
                output={"reason": str(exc)},
            )

        if 200 <= status < 300:
            context.audit.log_outcome(
                intent, "ok", {"platform": platform, "status": status},
            )
            return NeuronResult(
                ok=True,
                output={"platform": platform, "action": action,
                        "status": status},
            )
        if status in (401, 403):
            ticket = await self.escalate(
                Blocker(
                    kind="approval",
                    detail=f"{platform} ads token rejected ({status})",
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
        if status in (451, 423):
            # 451 (legal) / 423 (locked) shape — ad account locked.
            ticket = await self.escalate(
                Blocker(
                    kind="approval",
                    detail=f"{platform} ad account locked ({status})",
                ),
                context,
            )
            context.audit.log_outcome(
                intent, "escalated", {"ticket": ticket.id},
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="account_disabled",
                output={"ticket": ticket.id},
            )
        context.audit.log_outcome(
            intent, "fail", {"status": status},
        )
        return NeuronResult(
            ok=False, error_code="ads_api_unavailable",
            output={"status": status},
        )


# Placeholder endpoints — production wires real URLs (and the x402
# client handles auth + payment for impressions / clicks).
_DEFAULT_ENDPOINTS: dict[str, str] = {
    "meta": "https://graph.facebook.com/v18.0/act_<account>/campaigns",
    "google": "https://googleads.googleapis.com/v15/customers/<id>/campaigns:mutate",
    "x": "https://ads-api.x.com/12/accounts/<id>/campaigns",
    "linkedin": "https://api.linkedin.com/rest/adCampaigns",
}
