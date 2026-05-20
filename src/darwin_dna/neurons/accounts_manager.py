"""accounts.manager — sign up for services, refresh tokens, hold cookies
(docs/engineering/04).

The neuron that owns the lifecycle of an account-with-a-third-party:
- Sign up (drives browser.operator under the hood, OR calls a signup API
  via ctx.http when available).
- Persist the resulting credentials to the scoped vault.
- Refresh expired tokens by re-running the auth flow.

Layer 0 ships the contract + the credential persistence flow; the
actual signup workflows are domain-specific (one per service) and live
in the soma (not the genome).
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
    vault_put,
)


class AccountsManager(BaseNeuron):
    name = "accounts.manager"
    version = "1.0.0"
    capability_tags = frozenset(
        {"accounts.manage", "web.signup", "web.operate", "vault.write"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.02, usd_p95=0.20,
        latency_p50_ms=5000, latency_p95_ms=60000,
        escalation_probability=0.10,
    )
    failure_modes = [
        FailureMode(
            code="signup_blocked",
            description="signup form blocked (captcha, KYC, manual review)",
            escalation_hint="owner solves captcha / completes KYC",
        ),
        FailureMode(
            code="auth_required",
            description="token expired and refresh failed",
            escalation_hint="owner re-issues the credential",
        ),
        FailureMode(
            code="provider_unavailable",
            description="signup API unreachable / rate limited",
            escalation_hint="not human-actionable; retry later",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        action = task.payload.get("action", "signup")
        service = task.payload.get("service", "")
        return Plan(
            steps=[f"{action} on {service or '<service>'}"],
            est_cost=self.cost_estimate,
            payload={
                "action": action,
                "service": service,
                "endpoint": task.payload.get("endpoint", ""),
                "browser_steps": task.payload.get("browser_steps", []),
                "credentials_brief": task.payload.get("credentials_brief", {}),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        action = plan.payload.get("action", "signup")
        service = plan.payload.get("service", "")
        intent = context.audit.log_intent(
            "neuron.accounts.manager",
            {"action": action, "service": service},
        )

        # Path A: API signup via x402 HTTP client.
        endpoint = plan.payload.get("endpoint")
        http = getattr(context, "http", None)
        outcome: dict | None = None
        if http is not None and endpoint:
            try:
                resp = http.request(
                    "POST", endpoint,
                    json=plan.payload.get("credentials_brief", {}),
                )
                status = int(getattr(resp, "status_code", 0))
                if 200 <= status < 300:
                    outcome = {"ok": True, "via": "api", "status": status}
                elif status in (401, 403, 429):
                    ticket = await self.escalate(
                        Blocker(
                            kind="approval",
                            detail=f"{service} signup returned {status}",
                        ),
                        context,
                    )
                    context.audit.log_outcome(
                        intent, "escalated", {"ticket": ticket.id},
                    )
                    return NeuronResult(
                        ok=False, escalated=True,
                        error_code="auth_required",
                        output={"ticket": ticket.id},
                    )
                else:
                    outcome = {"ok": False, "status": status}
            except Exception as exc:
                context.audit.log_outcome(intent, "fail", {"reason": str(exc)})
                return NeuronResult(
                    ok=False, error_code="provider_unavailable",
                    output={"reason": str(exc)},
                )

        # Path B: drive browser.operator's steps via the browser substrate.
        browser_steps = plan.payload.get("browser_steps", [])
        if outcome is None and browser_steps:
            substrate = getattr(context, "browser", None)
            if substrate is None:
                context.audit.log_outcome(
                    intent, "fail", {"reason": "no browser substrate"},
                )
                return NeuronResult(
                    ok=False, error_code="provider_unavailable",
                )
            res = substrate.run(browser_steps)
            if res.get("blocked") == "captcha":
                ticket = await self.escalate(
                    Blocker(
                        kind="captcha",
                        detail=res.get("detail", "captcha wall"),
                    ),
                    context,
                )
                context.audit.log_outcome(
                    intent, "escalated", {"ticket": ticket.id},
                )
                return NeuronResult(
                    ok=False, escalated=True, error_code="signup_blocked",
                    output={"ticket": ticket.id},
                )
            if not res.get("ok"):
                context.audit.log_outcome(
                    intent, "fail", {"reason": res.get("detail")},
                )
                return NeuronResult(
                    ok=False, error_code="signup_blocked",
                    output={"reason": res.get("detail")},
                )
            outcome = {"ok": True, "via": "browser", "data": res.get("data", {})}

        if outcome is None:
            context.audit.log_outcome(
                intent, "fail", {"reason": "no signup path"},
            )
            return NeuronResult(
                ok=False, error_code="provider_unavailable",
                output={"reason": "no signup endpoint or browser steps"},
            )

        # Persist any returned credentials to the scoped vault.
        creds = plan.payload.get("credentials_brief", {})
        token = (outcome.get("data") or {}).get("token") or creds.get("token")
        if token and context.vault is not None:
            vault_put(
                context.vault,
                f"accounts:{service or 'unknown'}:token",
                str(token),
                tier="scoped",
                scope="accounts.manage",
            )

        context.audit.log_outcome(
            intent, "ok",
            {"action": action, "service": service, "via": outcome.get("via")},
        )
        return NeuronResult(
            ok=True,
            output={
                "service": service,
                "action": action,
                "via": outcome.get("via"),
            },
        )
