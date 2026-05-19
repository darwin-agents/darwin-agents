"""content.writer — long-form text generation (docs/engineering/04).

Reasoning-tier, routed through the platform gateway (no model name here). The
starter content neuron the founding content+affiliate strategy leans on.
"""

from __future__ import annotations

from ..machinery.llm_router import Tier
from ._base import (
    BaseNeuron,
    CostEstimate,
    FailureMode,
    NeuronContext,
    NeuronResult,
    Plan,
    Task,
)


class ContentWriter(BaseNeuron):
    name = "content.writer"
    version = "1.0.0"
    capability_tags = frozenset({"content.long_form", "content.write"})
    cost_estimate = CostEstimate(
        usd_p50=0.02, usd_p95=0.08, latency_p50_ms=4000, latency_p95_ms=15000,
        escalation_probability=0.0,
    )
    failure_modes = [
        FailureMode(
            code="llm_unavailable",
            description="reasoning tier exhausted or transport failed",
            escalation_hint="not human-actionable; back off / tier-down",
        ),
        FailureMode(
            code="empty_generation",
            description="model returned no usable text",
            escalation_hint="retry once with a tightened brief, else skip",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        topic = task.payload.get("topic", "untitled")
        target_words = int(task.payload.get("target_words", 800))
        return Plan(
            steps=[f"draft ~{target_words}w on {topic!r}", "self-check structure"],
            est_cost=self.cost_estimate,
            notes=f"quality={task.quality}",
            payload={"topic": topic, "target_words": target_words,
                     "quality": task.quality},
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        topic = plan.payload.get("topic", "untitled")
        intent = context.audit.log_intent(
            "neuron.content.writer", {"plan": plan.steps, "topic": topic}
        )
        try:
            resp = context.llm.complete(
                Tier.REASONING,
                {
                    "task": "write_long_form",
                    "topic": topic,
                    "target_words": plan.payload.get("target_words", 800),
                    "quality": plan.payload.get("quality", "standard"),
                },
            )
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"error": str(exc)})
            return NeuronResult(ok=False, error_code="llm_unavailable")

        text = resp.get("text", "")
        if not text.strip():
            context.audit.log_outcome(intent, "fail", {"reason": "empty"})
            return NeuronResult(ok=False, error_code="empty_generation")

        words = len(text.split())
        cost = float(resp.get("cost_usdc", self.cost_estimate.usd_p50))
        context.audit.log_outcome(
            intent, "ok", {"words": words, "cost_usdc": cost}
        )
        return NeuronResult(
            ok=True,
            cost_actual_usdc=cost,
            output={
                "text": text,
                "words": words,
                "quality_score": float(resp.get("quality_score", 0.6)),
            },
        )
