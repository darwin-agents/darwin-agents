"""analytics.measurer — reads metrics + computes ROI (docs/engineering/04).

The Darwin needs feedback on its own actions to evolve well (P5 + doc 06
fitness). This neuron is the read-only side of the measurement loop: it
pulls metrics from configured analytics endpoints (Google Analytics,
affiliate network dashboards, X analytics) via the x402 HTTP client, then
synthesises an ROI dict the reflect node can consume.

Layer 0 ships the contract + LLM-driven synthesis stub; production wires
real analytics clients behind the same NeuronContext.http surface.
"""

from __future__ import annotations

import json

from ..machinery.llm_router import Tier, acomplete
from ._base import (
    BaseNeuron,
    CostEstimate,
    FailureMode,
    NeuronContext,
    NeuronResult,
    Plan,
    Task,
)


class AnalyticsMeasurer(BaseNeuron):
    name = "analytics.measurer"
    version = "1.0.0"
    capability_tags = frozenset(
        {"analytics.measure", "web.read", "web.operate"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.005, usd_p95=0.05,
        latency_p50_ms=2000, latency_p95_ms=15000,
        escalation_probability=0.02,
    )
    failure_modes = [
        FailureMode(
            code="analytics_unavailable",
            description="metrics endpoint refused or returned nothing usable",
            escalation_hint="not human-actionable; rely on cached prior metrics",
        ),
        FailureMode(
            code="no_endpoint",
            description="no analytics endpoint configured for this scope",
            escalation_hint="owner wires an analytics source + credentials",
        ),
    ]

    def __init__(self) -> None:
        # Phase 1: the supervisor pins a real analytics endpoint here
        # post-boot via ``default_endpoint``. The strategy can still
        # override per-task; absence keeps Phase-0 behaviour.
        super().__init__()
        self.default_endpoint: str | None = None

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        scope = task.payload.get("scope", "site")
        return Plan(
            steps=[
                f"fetch metrics for scope={scope}",
                "synthesise ROI breakdown",
            ],
            est_cost=self.cost_estimate,
            payload={
                "scope": scope,
                "window_days": int(task.payload.get("window_days", 7)),
                "endpoint": task.payload.get("endpoint", ""),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        scope = plan.payload.get("scope", "site")
        # Endpoint resolution priority (Phase 1):
        #   1. Per-task endpoint (the strategy's explicit override).
        #   2. Platform default pinned by the supervisor (``default_endpoint``).
        #   3. Empty → no-endpoint failure path (Phase-0 behaviour).
        endpoint = (
            plan.payload.get("endpoint", "")
            or (getattr(self, "default_endpoint", None) or "")
        )
        intent = context.audit.log_intent(
            "neuron.analytics.measurer",
            {"scope": scope, "window_days": plan.payload.get("window_days")},
        )

        raw: dict | None = None
        http = getattr(context, "http", None)
        if http is not None and endpoint:
            try:
                resp = http.request(
                    "GET", endpoint,
                    params={
                        "window_days": plan.payload.get("window_days", 7),
                        "scope": scope,
                    },
                )
                status = int(getattr(resp, "status_code", 0))
                if 200 <= status < 300:
                    body = getattr(resp, "text", "") or "{}"
                    try:
                        raw = json.loads(body) if isinstance(body, str) else dict(body)
                    except (TypeError, ValueError):
                        raw = {"raw": str(body)}
                else:
                    context.audit.log_outcome(
                        intent, "fail", {"reason": f"status={status}"}
                    )
                    return NeuronResult(
                        ok=False, error_code="analytics_unavailable",
                        output={"status": status},
                    )
            except Exception as exc:
                context.audit.log_outcome(
                    intent, "fail", {"reason": str(exc)}
                )
                return NeuronResult(
                    ok=False, error_code="analytics_unavailable",
                    output={"reason": str(exc)},
                )
        elif not endpoint:
            # No endpoint at all — return a no-data result, NOT an
            # escalation. Reflect can handle empty metrics; an escalation
            # storm from a misconfigured scope would be worse.
            context.audit.log_outcome(
                intent, "skipped", {"reason": "no endpoint configured"}
            )
            return NeuronResult(
                ok=False, error_code="no_endpoint",
                output={"scope": scope, "metrics": {}},
            )

        # Synthesise the ROI dict via the fast tier. Cheap; reflect will
        # discard the synthesis when the raw numbers are missing.
        try:
            resp = await acomplete(
                context.llm,
                Tier.FAST,
                {
                    "task": "analytics_synthesis",
                    "scope": scope,
                    "window_days": plan.payload.get("window_days", 7),
                    "raw_metrics": raw or {},
                },
            )
            text = resp.get("text", "")
            roi = float(resp.get("roi", 0.0))
            context.audit.log_outcome(
                intent, "ok",
                {"scope": scope, "roi": roi,
                 "have_raw": raw is not None},
            )
            return NeuronResult(
                ok=True,
                cost_actual_usdc=float(resp.get("cost_usdc",
                                                self.cost_estimate.usd_p50)),
                output={
                    "scope": scope,
                    "metrics": raw or {},
                    "roi": roi,
                    "synthesis": text,
                    "window_days": plan.payload.get("window_days", 7),
                },
            )
        except Exception as exc:
            context.audit.log_outcome(
                intent, "fail", {"reason": str(exc)}
            )
            # Don't lose the raw metrics if the LLM step failed.
            return NeuronResult(
                ok=False, error_code="analytics_unavailable",
                output={"scope": scope, "metrics": raw or {}, "reason": str(exc)},
            )
