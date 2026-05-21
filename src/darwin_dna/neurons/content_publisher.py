"""content.publisher — distribution to publishing channels (docs/engineering/04).

content.writer produces text; this neuron is the one that turns that text
into a *published artefact* on a distribution channel (WordPress, Medium,
Ghost, X, LinkedIn, RSS, etc.). It tries a stable API first via the x402
HTTP client when configured, then falls back to ``browser.operator`` for
the long tail of sites that don't expose an API; if neither is available
it escalates the owner so the publishing flow isn't a silent failure.

Layer 0 ships the neuron's contract + audit trail; production wires a real
publishing API client (or the browser.operator path) behind it.
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


class ContentPublisher(BaseNeuron):
    name = "content.publisher"
    version = "1.0.0"
    capability_tags = frozenset(
        {"content.publish", "web.publish", "web.operate"}
    )
    cost_estimate = CostEstimate(
        usd_p50=0.01, usd_p95=0.10,
        latency_p50_ms=3000, latency_p95_ms=20000,
        escalation_probability=0.05,
    )
    failure_modes = [
        FailureMode(
            code="no_channel",
            description="no publishing channel configured for this category",
            escalation_hint="owner adds a publishing destination + credentials",
        ),
        FailureMode(
            code="api_unavailable",
            description="publishing API refused, rate-limited, or unreachable",
            escalation_hint="not human-actionable; retry / fall back to browser",
        ),
        FailureMode(
            code="auth_required",
            description="publishing API needs fresh credentials (token expired)",
            escalation_hint="owner refreshes / re-issues the API token",
        ),
    ]

    def __init__(self) -> None:
        # Phase 1: the supervisor pins per-channel real endpoints here
        # post-boot via ``endpoint_overrides``. The strategy keeps
        # naming channels symbolically; the platform owns the URLs +
        # auth (carried by the x402 client). An empty dict preserves
        # Phase-0 behaviour (the symbolic ``_DEFAULT_ENDPOINTS`` apply).
        super().__init__()
        self.endpoint_overrides: dict[str, str] = {}

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        channel = task.payload.get("channel", "wordpress")
        url_hint = task.payload.get("destination_url", "")
        return Plan(
            steps=[
                f"publish to {channel}",
                f"target: {url_hint or '<auto>'}",
            ],
            est_cost=self.cost_estimate,
            payload={
                "channel": channel,
                "destination_url": url_hint,
                "text": task.payload.get("text", ""),
                "title": task.payload.get("title", ""),
                "tags": task.payload.get("tags", []),
            },
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        channel = plan.payload.get("channel", "wordpress")
        text = plan.payload.get("text", "")
        if not text.strip():
            return NeuronResult(
                ok=False, error_code="api_unavailable",
                output={"reason": "empty content"},
            )
        intent = context.audit.log_intent(
            "neuron.content.publisher",
            {"channel": channel, "title": plan.payload.get("title", "")},
        )

        # Path A: x402 HTTP client (doc 17). When configured, this is the
        # cheap path — pay-as-you-go API access without per-channel
        # credentials wired into the genome. The platform's adapter knows
        # how to settle 402 responses against this Darwin's wallet.
        http = getattr(context, "http", None)
        # Endpoint resolution priority (Phase 1):
        #   1. Per-task destination_url (the strategy's explicit override).
        #   2. Per-channel real endpoint pinned by the platform supervisor
        #      via ``self.endpoint_overrides`` (real WordPress/etc URL).
        #   3. The symbolic default — Phase-0 behaviour preserved.
        overrides = getattr(self, "endpoint_overrides", {}) or {}
        endpoint = (
            plan.payload.get("destination_url", "")
            or overrides.get(channel)
            or _DEFAULT_ENDPOINTS.get(channel)
        )
        if http is not None and endpoint:
            try:
                resp = http.request(
                    "POST",
                    endpoint,
                    json={
                        "title": plan.payload.get("title", ""),
                        "body": text,
                        "tags": plan.payload.get("tags", []),
                    },
                )
                status = int(getattr(resp, "status_code", 0))
                if 200 <= status < 300:
                    context.audit.log_outcome(
                        intent, "ok", {"channel": channel, "status": status}
                    )
                    return NeuronResult(
                        ok=True,
                        output={"channel": channel, "status": status,
                                "via": "api"},
                    )
                if status == 401 or status == 403:
                    ticket = await self.escalate(
                        Blocker(
                            kind="approval",
                            detail=f"{channel} API returned {status}; "
                            "owner must refresh credentials",
                        ),
                        context,
                    )
                    context.audit.log_outcome(
                        intent, "escalated", {"ticket": ticket.id}
                    )
                    return NeuronResult(
                        ok=False, escalated=True, error_code="auth_required",
                        output={"ticket": ticket.id, "channel": channel},
                    )
                # Other failure — fall through to path B.
                context.audit.log_outcome(
                    intent, "fail", {"reason": f"status={status}"}
                )
            except Exception as exc:
                context.audit.log_outcome(
                    intent, "fail", {"reason": str(exc)}
                )
                # Fall through to escalation; we don't blow up the genome.

        # Path B (fallback): no x402 client and no API path → escalate so
        # the owner can either configure a channel or accept manual
        # publication. Doc 04: a known gap is a structured request, not a
        # silent skip.
        ticket = await self.escalate(
            Blocker(
                kind="approval",
                detail=f"no publishing channel configured for {channel!r}; "
                "owner needs to wire an API or browser route",
            ),
            context,
        )
        context.audit.log_outcome(
            intent, "escalated", {"ticket": ticket.id, "channel": channel}
        )
        return NeuronResult(
            ok=False, escalated=True, error_code="no_channel",
            output={"ticket": ticket.id, "channel": channel},
        )


# Symbolic endpoints — production wires concrete URLs (and the x402
# client handles auth). The presence of an endpoint string is what gates
# Path A; tests inject overrides via task.payload["destination_url"].
_DEFAULT_ENDPOINTS: dict[str, str] = {
    "wordpress": "https://api.wordpress.com/v1.1/posts",
    "medium": "https://api.medium.com/v1/posts",
    "ghost": "https://example.ghost.io/api/admin/posts",
    "x": "https://api.x.com/2/tweets",
    "linkedin": "https://api.linkedin.com/v2/ugcPosts",
}
