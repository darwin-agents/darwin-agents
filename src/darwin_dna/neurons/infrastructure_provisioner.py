"""infrastructure.provisioner — platform-mediated provisioning
(docs/engineering/04, 07).

The only neuron that may ask the *platform* to create managed resources
(databases, buckets, vector stores, scheduled jobs). It never provisions
anything itself; it asks, and writes the resulting credentials back through
vault.writer's path (here: the injected vault).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

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


@runtime_checkable
class ProvisionPort(Protocol):
    """The platform control-plane provisioning API, abstracted."""

    def provision(self, kind: str, spec: dict) -> dict: ...  # -> {id, creds}


class InfrastructureProvisioner(BaseNeuron):
    name = "infrastructure.provisioner"
    version = "1.0.0"
    capability_tags = frozenset({"infra.provision"})
    cost_estimate = CostEstimate(
        usd_p50=0.0, usd_p95=0.0, latency_p50_ms=3000, latency_p95_ms=20000,
        escalation_probability=0.02,
    )
    failure_modes = [
        FailureMode(
            code="provision_denied",
            description="platform refused (quota, policy)",
            escalation_hint="owner may need to raise the resource quota",
        ),
    ]

    def __init__(self, provision: ProvisionPort):
        if not isinstance(provision, ProvisionPort):
            raise TypeError("infrastructure.provisioner requires a ProvisionPort")
        self._provision = provision

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        return Plan(
            steps=[f"request {task.payload.get('kind', '<kind>')}"],
            est_cost=self.cost_estimate,
            payload=dict(task.payload),
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        kind = plan.payload.get("kind")
        spec = plan.payload.get("spec", {})
        intent = context.audit.log_intent(
            "neuron.infra.provision", {"kind": kind, "spec": spec}
        )
        try:
            result = self._provision.provision(kind, spec)
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"error": str(exc)})
            ticket = await self.escalate(
                Blocker(kind="approval", detail=f"provision {kind} failed: {exc}"),
                context,
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="provision_denied",
                output={"ticket": ticket.id},
            )

        # Persist credentials through the centralized vault path, never inline.
        if context.vault is not None and "creds" in result:
            context.vault.put(f"infra:{kind}:{result.get('id', 'new')}",
                              str(result["creds"]))
        context.audit.log_outcome(intent, "ok", {"id": result.get("id")})
        return NeuronResult(
            ok=True,
            output={"id": result.get("id"), "kind": kind},
        )
