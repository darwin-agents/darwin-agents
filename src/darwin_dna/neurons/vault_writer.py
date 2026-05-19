"""vault.writer — centralized vault writes (docs/engineering/04, 16).

Other neurons request "remember this" through this neuron instead of writing
the vault directly. One write path = one audit point for everything secret
the Darwin chooses to persist.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ._base import (
    BaseNeuron,
    CostEstimate,
    FailureMode,
    NeuronContext,
    NeuronResult,
    Plan,
    Task,
)


@runtime_checkable
class VaultPort(Protocol):
    def put(self, key: str, value: str) -> None: ...


class VaultWriter(BaseNeuron):
    name = "vault.writer"
    version = "1.0.0"
    capability_tags = frozenset({"vault.write"})
    cost_estimate = CostEstimate(
        usd_p50=0.0, usd_p95=0.0, latency_p50_ms=200, latency_p95_ms=1500,
        escalation_probability=0.0,
    )
    failure_modes = [
        FailureMode(
            code="vault_unavailable",
            description="vault backend unreachable",
            escalation_hint="not human-actionable; retry, then surface to owner",
        ),
    ]

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        return Plan(
            steps=[f"store {task.payload.get('key', '<key>')!r}"],
            est_cost=self.cost_estimate,
            payload=dict(task.payload),
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        key = plan.payload.get("key")
        value = plan.payload.get("value")
        if not key or value is None:
            return NeuronResult(ok=False, error_code="vault_unavailable",
                                output={"reason": "key/value missing"})
        vault = context.vault
        if vault is None:
            return NeuronResult(ok=False, error_code="vault_unavailable")
        intent = context.audit.log_intent("neuron.vault.writer", {"key": key})
        try:
            vault.put(key, str(value))
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"error": str(exc)})
            return NeuronResult(ok=False, error_code="vault_unavailable")
        # Never log the secret value — only that a write happened, to which key.
        context.audit.log_outcome(intent, "ok", {"key": key})
        return NeuronResult(ok=True, output={"key": key})
