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
    vault_put,
)

_TIERS = {"public", "scoped", "sensitive"}


@runtime_checkable
class VaultPort(Protocol):
    # The genome contract is intentionally minimal: every vault backend can
    # ``put(key, value)``. The tier model (doc 16) rides on top via the
    # ``ScopedVaultView`` the orchestrator injects; ``vault_put`` bridges the
    # two so this neuron names a tier without depending on the concrete type.
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
        # The caller names the tier; an unrecognised/absent tier is treated as
        # ``scoped`` — the safe default is the *narrower* blast radius, never
        # public, and never an implicit sensitive write.
        tier = str(task.payload.get("tier", "scoped")).lower()
        payload = dict(task.payload)
        payload["tier"] = tier if tier in _TIERS else "scoped"
        return Plan(
            steps=[f"store {task.payload.get('key', '<key>')!r} "
                   f"@tier={payload['tier']}"],
            est_cost=self.cost_estimate,
            payload=payload,
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
        tier = plan.payload.get("tier", "scoped")
        scope = plan.payload.get("scope")
        # The key alone is logged, plus the tier (blast radius is not a
        # secret); the value never is (P7, doc 16).
        intent = context.audit.log_intent(
            "neuron.vault.writer", {"key": key, "tier": tier}
        )
        try:
            vault_put(vault, key, str(value), tier=tier, scope=scope)
        except Exception as exc:
            context.audit.log_outcome(intent, "fail", {"error": str(exc)})
            return NeuronResult(ok=False, error_code="vault_unavailable")
        # Never log the secret value — only that a write happened, to which key.
        context.audit.log_outcome(intent, "ok", {"key": key})
        return NeuronResult(ok=True, output={"key": key})
