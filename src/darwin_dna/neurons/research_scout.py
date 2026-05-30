"""research.scout — web search + synthesis (docs/engineering/04).

The Darwin's "open the browser, look something up, summarise it"
capability. Two sub-paths in production:

1. **Search API** via x402 HTTP (Brave, Google CSE, You.com, etc.). Cheap,
   structured. Used when the platform has an API key in the vault and an
   endpoint configured.
2. **Browser fallback** via browser.operator for sites that don't expose
   search APIs. Slower, captcha-prone — kept as a fallback rather than
   the default.

Layer 0 ships path 1 + the synthesis LLM call. Path 2 is reachable only
when the orchestrator's plan combines research.scout with a downstream
browser.operator dispatch.
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


class ResearchScout(BaseNeuron):
    name = "research.scout"
    version = "1.0.0"
    capability_tags = frozenset(
        {"research.scout", "web.read", "web.operate"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.01, usd_p95=0.08,
        latency_p50_ms=3000, latency_p95_ms=20000,
        escalation_probability=0.02,
    )
    failure_modes = [
        FailureMode(
            code="search_unavailable",
            description="search endpoint refused or returned nothing usable",
            escalation_hint="not human-actionable; try with broader query",
        ),
        FailureMode(
            code="no_endpoint",
            description="no search endpoint configured",
            escalation_hint="owner wires a search API endpoint",
        ),
        FailureMode(
            code="empty_synthesis",
            description="LLM synthesis produced no usable summary",
            escalation_hint="not human-actionable; retry once with raw results",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        query = task.payload.get("query", "")
        return Plan(
            steps=[
                f"search: {query!r}",
                "synthesise findings",
            ],
            est_cost=self.cost_estimate,
            payload={
                "query": query,
                "k": int(task.payload.get("k", 5)),
                "endpoint": task.payload.get(
                    "endpoint", "https://api.search.brave.com/res/v1/web/search"
                ),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        query = plan.payload.get("query", "")
        endpoint = plan.payload.get("endpoint", "")
        if not query.strip():
            return NeuronResult(
                ok=False, error_code="search_unavailable",
                output={"reason": "empty query"},
            )
        intent = context.audit.log_intent(
            "neuron.research.scout",
            {"query": query, "k": plan.payload.get("k", 5)},
        )

        results: list[dict] = []
        http = getattr(context, "http", None)
        if http is not None and endpoint:
            try:
                resp = http.request(
                    "GET", endpoint,
                    params={"q": query, "count": plan.payload.get("k", 5)},
                )
                status = int(getattr(resp, "status_code", 0))
                if 200 <= status < 300:
                    body = getattr(resp, "text", "") or "{}"
                    try:
                        data = json.loads(body) if isinstance(body, str) else dict(body)
                    except (TypeError, ValueError):
                        data = {}
                    results = list(data.get("results", data.get("web", {}).get("results", [])))[
                        : plan.payload.get("k", 5)
                    ]
                else:
                    context.audit.log_outcome(
                        intent, "fail", {"reason": f"status={status}"}
                    )
                    return NeuronResult(
                        ok=False, error_code="search_unavailable",
                        output={"status": status},
                    )
            except Exception as exc:
                context.audit.log_outcome(
                    intent, "fail", {"reason": str(exc)}
                )
                return NeuronResult(
                    ok=False, error_code="search_unavailable",
                    output={"reason": str(exc)},
                )
        elif not endpoint:
            context.audit.log_outcome(
                intent, "skipped", {"reason": "no endpoint configured"}
            )
            return NeuronResult(
                ok=False, error_code="no_endpoint",
                output={"query": query, "results": []},
            )

        # Synthesise via fast tier — the user gets a summary, not raw rows.
        try:
            resp = await acomplete(
                context.llm,
                Tier.FAST,
                {
                    "task": "research_synthesis",
                    "query": query,
                    "results": results,
                },
            )
            text = resp.get("text", "")
            if not text.strip():
                context.audit.log_outcome(
                    intent, "fail", {"reason": "empty synthesis"}
                )
                return NeuronResult(
                    ok=False, error_code="empty_synthesis",
                    output={"query": query, "results": results},
                )
            cost = float(resp.get("cost_usdc", self.cost_estimate.usd_p50))
            context.audit.log_outcome(
                intent, "ok",
                {"query": query, "n_results": len(results), "cost_usdc": cost},
            )
            return NeuronResult(
                ok=True,
                cost_actual_usdc=cost,
                output={
                    "query": query,
                    "summary": text,
                    "results": results,
                    "n_results": len(results),
                },
            )
        except Exception as exc:
            context.audit.log_outcome(
                intent, "fail", {"reason": str(exc)}
            )
            return NeuronResult(
                ok=False, error_code="search_unavailable",
                output={"query": query, "results": results, "reason": str(exc)},
            )
