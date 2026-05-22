"""support.responder — responds to DMs / emails / support tickets
(docs/engineering/04).

Triage + reply for customer support across channels. Uses ctx.llm
(REASONING tier) to compose a reply, then ctx.http to deliver via the
configured channel (Zendesk / Intercom / email / Twitter DMs).
Escalates to the owner when the LLM's confidence is below threshold or
the message looks like an abuse / legal / billing-dispute issue that
needs a human.
"""

from __future__ import annotations


from ..machinery.llm_router import Tier
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


_HUMAN_NEEDED_KEYWORDS = (
    "legal",
    "lawsuit",
    "refund",
    "chargeback",
    "lawyer",
    "attorney",
    "consumer protection",
    "report you to",
    "police",
    "scam",
    "fraud",
)


class SupportResponder(BaseNeuron):
    name = "support.responder"
    version = "1.0.0"
    capability_tags = frozenset(
        {"support.respond", "web.operate", "content.write"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.01, usd_p95=0.08,
        latency_p50_ms=3000, latency_p95_ms=15000,
        escalation_probability=0.10,
    )
    failure_modes = [
        FailureMode(
            code="reply_send_failed",
            description="channel API refused the reply (rate limit, auth)",
            escalation_hint="not human-actionable; back off / retry",
        ),
        FailureMode(
            code="needs_human",
            description="message classified as needing human review",
            escalation_hint="owner reviews + responds",
        ),
        FailureMode(
            code="empty_reply",
            description="LLM returned no usable reply",
            escalation_hint="not human-actionable; retry once",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        channel = task.payload.get("channel", "email")
        return Plan(
            steps=[f"reply on {channel}"],
            est_cost=self.cost_estimate,
            payload={
                "channel": channel,
                "thread_id": task.payload.get("thread_id", ""),
                "incoming_message": task.payload.get("incoming_message", ""),
                "context_brief": task.payload.get("context_brief", ""),
                "endpoint": task.payload.get("endpoint", ""),
                "confidence_threshold": float(
                    task.payload.get("confidence_threshold", 0.6)
                ),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        channel = plan.payload.get("channel", "email")
        incoming = plan.payload.get("incoming_message", "")
        if not incoming.strip():
            return NeuronResult(
                ok=False, error_code="empty_reply",
                output={"reason": "no incoming message"},
            )
        intent = context.audit.log_intent(
            "neuron.support.responder",
            {"channel": channel,
             "thread_id": plan.payload.get("thread_id", "")},
        )

        # Pre-filter: messages mentioning legal / refund / fraud go
        # straight to escalation — never reply autonomously.
        low = incoming.lower()
        if any(kw in low for kw in _HUMAN_NEEDED_KEYWORDS):
            ticket = await self.escalate(
                Blocker(
                    kind="approval",
                    detail=f"{channel} message needs human (legal/refund/fraud kw)",
                ),
                context,
            )
            context.audit.log_outcome(
                intent, "escalated", {"ticket": ticket.id, "reason": "keyword"},
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="needs_human",
                output={"ticket": ticket.id},
            )

        # Compose reply via reasoning tier.
        try:
            resp = context.llm.complete(
                Tier.REASONING,
                {
                    "task": "support_reply",
                    "channel": channel,
                    "incoming_message": incoming,
                    "context_brief": plan.payload.get("context_brief", ""),
                },
            )
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"reason": str(exc)})
            return NeuronResult(
                ok=False, error_code="empty_reply",
                output={"reason": str(exc)},
            )
        reply_text = (resp.get("text") or "").strip()
        confidence = float(resp.get("confidence", 0.7))
        if not reply_text:
            context.audit.log_outcome(
                intent, "fail", {"reason": "empty LLM reply"},
            )
            return NeuronResult(
                ok=False, error_code="empty_reply",
                output={"channel": channel},
            )
        threshold = plan.payload.get("confidence_threshold", 0.6)
        if confidence < threshold:
            ticket = await self.escalate(
                Blocker(
                    kind="approval",
                    detail=(
                        f"low-confidence reply on {channel} "
                        f"(conf={confidence:.2f} < {threshold})"
                    ),
                ),
                context,
            )
            context.audit.log_outcome(
                intent, "escalated",
                {"ticket": ticket.id, "confidence": confidence},
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="needs_human",
                output={"ticket": ticket.id, "confidence": confidence},
            )

        # Deliver via ctx.http to the channel endpoint.
        endpoint = plan.payload.get("endpoint", "")
        http = getattr(context, "http", None)
        if http is None or not endpoint:
            # No channel wired — return the composed reply so the
            # orchestrator can hand it to a separate delivery neuron.
            context.audit.log_outcome(
                intent, "ok",
                {"channel": channel, "delivered": False,
                 "confidence": confidence},
            )
            return NeuronResult(
                ok=True,
                cost_actual_usdc=float(resp.get(
                    "cost_usdc", self.cost_estimate.usd_p50
                )),
                output={
                    "channel": channel,
                    "reply": reply_text,
                    "delivered": False,
                    "confidence": confidence,
                },
            )
        try:
            send = http.request(
                "POST", endpoint,
                json={
                    "thread_id": plan.payload.get("thread_id", ""),
                    "body": reply_text,
                },
            )
            status = int(getattr(send, "status_code", 0))
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"reason": str(exc)})
            return NeuronResult(
                ok=False, error_code="reply_send_failed",
                output={"reason": str(exc)},
            )
        if 200 <= status < 300:
            context.audit.log_outcome(
                intent, "ok",
                {"channel": channel, "status": status, "delivered": True},
            )
            return NeuronResult(
                ok=True,
                cost_actual_usdc=float(resp.get(
                    "cost_usdc", self.cost_estimate.usd_p50
                )),
                output={
                    "channel": channel, "reply": reply_text,
                    "delivered": True, "status": status,
                    "confidence": confidence,
                },
            )
        context.audit.log_outcome(
            intent, "fail", {"status": status},
        )
        return NeuronResult(
            ok=False, error_code="reply_send_failed",
            output={"status": status},
        )
