"""The neuron contract (docs/engineering/04, P3: the neuron is the unit of
capability).

Every neuron — human-written or Darwin-evolved — implements this one
interface. Without a stable contract the orchestrator cannot reason about
which neuron to call and the self-modification engine cannot compare
alternatives. The contract is enforced at *load time* by
``machinery/neuron_loader.py``; a non-conforming neuron never runs.
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field


class CostEstimate(BaseModel):
    """Declared per-invocation cost. The orchestrator plans against p50; the
    treasury policy refuses plans whose p95 blows the safety margin."""

    model_config = {"frozen": True}

    usd_p50: float = Field(ge=0)
    usd_p95: float = Field(ge=0)
    latency_p50_ms: int = Field(ge=0)
    latency_p95_ms: int = Field(ge=0)
    escalation_probability: float = Field(ge=0, le=1)


class FailureMode(BaseModel):
    """A known class of failure, mapped to an escalation hint. One obvious
    failure mode per neuron keeps escalation routing unambiguous (04)."""

    model_config = {"frozen": True}

    code: str  # stable error *class* (recurrence is matched on this, not msg)
    description: str
    escalation_hint: str


class Task(BaseModel):
    model_config = {"frozen": True}

    id: str
    kind: str
    required_tags: frozenset[str] = frozenset()
    quality: str = "standard"  # "draft" | "standard" | "high"
    category: str = "content.blog"
    payload: dict[str, Any] = Field(default_factory=dict)


class Plan(BaseModel):
    model_config = {"frozen": True}

    steps: list[str]
    est_cost: CostEstimate
    notes: str = ""
    # Carries the plan's own structured inputs forward to ``execute`` so the
    # Protocol stays uniform (plan→execute) even for neurons like
    # payments.facilitator that need the full task, not just prose steps.
    payload: dict[str, Any] = Field(default_factory=dict)


class NeuronResult(BaseModel):
    model_config = {"frozen": True}

    ok: bool
    output: dict[str, Any] = Field(default_factory=dict)
    cost_actual_usdc: float = 0.0
    escalated: bool = False
    error_code: str | None = None  # one of the neuron's FailureMode.code values


class Blocker(BaseModel):
    model_config = {"frozen": True}

    kind: str  # "captcha" | "kyc" | "approval" | "spend_approval"
    detail: str


class EscalationTicket(BaseModel):
    model_config = {"frozen": True}

    id: str
    neuron: str
    blocker: Blocker
    question: str
    created_at: float = Field(default_factory=time.time)


class NeuronContext:
    """The scoped capabilities a neuron is handed for one invocation. A
    neuron sees *only* what is in here — never the DNA, the treasury key,
    other neurons' state, or the orchestrator's full memory (04, isolation)."""

    def __init__(
        self,
        *,
        darwin_id: str,
        llm,  # LLMRouter
        audit,  # AuditLogger
        vault=None,  # scoped VaultPort | None
        lessons=None,  # read-only list[Lesson] relevant to this neuron
        now=time.time,
        http=None,  # X402PaymentClient | HTTPClient | None (doc 17)
        sandbox=None,  # SandboxClient | None (doc 14)
        content_classifier=None,  # ContentClassifier | None (doc 10)
    ):
        self.darwin_id = darwin_id
        self.llm = llm
        self.audit = audit
        self.vault = vault
        self.lessons = lessons or []
        self.now = now
        # x402-capable HTTP client (doc 17). Neurons whose capability tags
        # overlap {"web.operate", "web.publish"} receive this from
        # _build_neuron_context; others get None (plain HTTP, no auto-402).
        # When present, callers issue ``context.http.request(method, url,
        # **kwargs)`` and 402 interception + wallet settlement is transparent.
        self.http = http
        # Sandbox client (doc 14). Only neurons whose capability_tags include
        # ``compute.run`` receive it; everyone else gets None and may not run
        # arbitrary code. Future eval_harness and Path-C neurons consume this.
        self.sandbox = sandbox
        # Outbound content safety classifier (doc 10). Granted to publishing
        # neurons. The ContentSafetyGate runs its deterministic backstop even
        # when this is None, so outbound content is never wholly unscreened.
        self.content_classifier = content_classifier


@runtime_checkable
class Neuron(Protocol):
    name: str
    version: str
    capability_tags: frozenset[str]
    cost_estimate: CostEstimate
    failure_modes: list[FailureMode]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        """Look at the task, return a plan. Cheap. No side effects."""

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        """Do the work. Side effects allowed."""

    async def escalate(
        self, blocker: Blocker, context: NeuronContext
    ) -> EscalationTicket:
        """When stuck, file a ticket for the owner (P4)."""


class BaseNeuron:
    """Optional convenience base. Provides the default ``escalate`` (file a
    ticket, never raise) so concrete neurons only implement ``plan`` and
    ``execute``. Subclassing is not required — conformance to the Protocol
    is what the loader checks."""

    name: str = "base"
    version: str = "0.0.0"
    capability_tags: frozenset[str] = frozenset()
    cost_estimate: CostEstimate = CostEstimate(
        usd_p50=0.0, usd_p95=0.0, latency_p50_ms=0, latency_p95_ms=0,
        escalation_probability=0.0,
    )
    failure_modes: list[FailureMode] = []

    async def escalate(
        self, blocker: Blocker, context: NeuronContext
    ) -> EscalationTicket:
        ticket = EscalationTicket(
            id=f"esc-{self.name}-{int(context.now()*1000)}",
            neuron=self.name,
            blocker=blocker,
            question=(
                f"{self.name} is blocked on {blocker.kind}: {blocker.detail}. "
                f"Owner action needed to proceed."
            ),
        )
        context.audit.log_event(
            "escalation_filed",
            {"neuron": self.name, "blocker": blocker.model_dump(), "ticket": ticket.id},
        )
        return ticket


def vault_put(
    vault: Any,
    key: str,
    value: str,
    *,
    tier: str = "scoped",
    scope: str | None = None,
) -> None:
    """Write a secret through whatever vault handle the neuron was given,
    naming its tier explicitly (docs/engineering/16).

    In production ``context.vault`` is a ``ScopedVaultView`` that enforces the
    public/scoped/sensitive model and takes ``tier=``/``scope=``. The neuron
    must not hard-depend on that concrete type — the genome ``VaultPort``
    contract only promises ``put(key, value)`` — so this degrades to the bare
    signature for a plain backend. Either way the tier is *stated* at the
    write site, never inferred three nodes later."""
    from ..machinery.vault import VaultTier

    try:
        vault.put(key, value, tier=VaultTier(tier), scope=scope)
    except TypeError:
        # A non-tiered backend: still write, just without the tier metadata.
        vault.put(key, value)
