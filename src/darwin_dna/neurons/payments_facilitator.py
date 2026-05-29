"""payments.facilitator — the ONLY treasury-write neuron (docs/engineering/04, 05).

Every spend in the whole organism funnels here, and this neuron always runs
the spend through ``treasury_policy.evaluate`` *before* the wallet can sign.
That is why the policy is enforced in exactly one place. The neuron loader
guarantees no other neuron may even declare the ``treasury.write`` capability.

The wallet itself is an injected port (real: Coinbase Agentic Wallet with its
own independent spend-limit enforcement; fake: in-memory ledgered wallet). The
private key never enters this process.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..constitution import SpendIntent
from ..machinery.treasury_policy import (
    StrategyEnvelope,
    TreasuryState,
    Verdict,
    evaluate,
)
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
class WalletPort(Protocol):
    """The per-Darwin wallet. The genome only ever asks it to *read state* and
    *send an already-policy-approved payment*."""

    def balance_usdc(self) -> float: ...

    def spent_today_usdc(self) -> float: ...

    def spent_lifetime_usdc(self) -> float: ...

    def recent_spend_usdc(self) -> tuple[float, ...]: ...

    def pay(self, *, to: str, amount_usdc: float, memo: str) -> dict: ...


class PaymentsFacilitator(BaseNeuron):
    name = "payments.facilitator"  # MUST match neuron_loader.SOLE_TREASURY_NEURON
    version = "1.0.0"
    capability_tags = frozenset({"treasury.write", "payments.send"})
    cost_estimate = CostEstimate(
        usd_p50=0.001, usd_p95=0.01, latency_p50_ms=1500, latency_p95_ms=8000,
        escalation_probability=0.05,
    )
    failure_modes = [
        FailureMode(
            code="policy_refused",
            description="treasury policy refused the spend",
            escalation_hint="not human-actionable; the spend is simply not allowed",
        ),
        FailureMode(
            code="policy_escalated",
            description="treasury policy requires owner approval",
            escalation_hint="owner approves or rejects the payment",
        ),
        FailureMode(
            code="wallet_error",
            description="wallet rejected or failed to settle",
            escalation_hint="not human-actionable; retry, then surface to owner",
        ),
    ]

    def __init__(self, wallet: WalletPort):
        if not isinstance(wallet, WalletPort):
            raise TypeError("payments.facilitator requires a WalletPort")
        self._wallet = wallet

    async def plan(self, task: Task, context: NeuronContext) -> Plan:
        amt = float(task.payload.get("amount_usdc", 0.0))
        # Thread the full payment inputs through the Plan so execute() needs
        # nothing but (plan, context) — the uniform Protocol shape.
        return Plan(
            steps=[f"policy-check ${amt:.4f}", "sign+send if allowed"],
            est_cost=self.cost_estimate,
            payload={"category": task.category, **task.payload},
        )

    def _state(self, payload: dict) -> TreasuryState:
        env = payload.get("envelope")
        envelope = StrategyEnvelope(**env) if env else None
        # Owner soft caps (doc 08 POST /platform/wallets/{id}/policy) flow
        # from the platform into the wallet, and from the wallet into the
        # policy engine. Optional capability — wallets that do not expose
        # ``owner_soft_caps`` (older fakes / minimal real adapters) simply
        # don't get the layer-2 check, and the wallet's own ceiling still
        # refuses the spend. Either way the constitution's HARD_CAPS hold.
        soft_caps = None
        getter = getattr(self._wallet, "owner_soft_caps", None)
        if callable(getter):
            try:
                soft_caps = getter()
            except Exception:
                soft_caps = None
        # Owner pre-approvals (doc 17 §"What about a Darwin paying a human?"
        # — the "Upwork escrow under $50" auto-allow pattern). Optional
        # capability — wallets without ``owner_preapprovals`` get an empty
        # tuple, which keeps the legacy "every freelancer / over-threshold
        # spend escalates" behaviour.
        pre_approvals: tuple = ()
        pre_getter = getattr(self._wallet, "owner_preapprovals", None)
        if callable(pre_getter):
            try:
                pre_approvals = tuple(pre_getter() or ())
            except Exception:
                pre_approvals = ()
        # ``now_ts`` from the wallet's clock if it exposes one; otherwise
        # treat as +inf so any rule with an ``expires_at`` is considered
        # expired (fail-closed). Rules without an ``expires_at`` are
        # unaffected — they don't reference now_ts at all. The trade-off:
        # surprise the owner with one escalation rather than honour a
        # stale pre-approval they thought had elapsed.
        now_ts = float("inf")
        clock = getattr(self._wallet, "_clock", None)
        if callable(clock):
            try:
                now_ts = float(clock())
            except Exception:
                now_ts = float("inf")
        return TreasuryState(
            balance_usdc=self._wallet.balance_usdc(),
            spent_today_usdc=self._wallet.spent_today_usdc(),
            spent_lifetime_usdc=self._wallet.spent_lifetime_usdc(),
            recent_spend_usdc=tuple(self._wallet.recent_spend_usdc()),
            envelope=envelope,
            soft_caps=soft_caps,
            pre_approvals=pre_approvals,
            now_ts=now_ts,
        )

    async def execute(self, plan: Plan, context: NeuronContext) -> NeuronResult:
        p = plan.payload
        intent = SpendIntent(
            amount_usdc=float(p["amount_usdc"]),
            counterparty=p["counterparty"],
            counterparty_category=p.get("counterparty_category", "unknown"),
            jurisdiction=p.get("jurisdiction"),
            category=p.get("category", "content.blog"),
            strategy_id=p.get("strategy_id"),
            est_p95_usdc=p.get("est_p95_usdc"),
        )
        state = self._state(p)

        # WRITE-BEFORE-ACT: the policy decision and its rationale are logged
        # before any money can move (docs/engineering/18 hot path #4).
        audit_id = context.audit.log_intent(
            "payment.intent",
            {"intent": intent.model_dump(), "balance": state.balance_usdc},
        )
        decision = evaluate(intent, state)
        context.audit.log_event(
            "payment.policy_decision",
            {"audit_id": audit_id, "decision": decision.model_dump()},
        )

        if decision.verdict is Verdict.REFUSE:
            context.audit.log_outcome(
                audit_id, "refused", {"reason": decision.reason, "layer": decision.layer}
            )
            return NeuronResult(
                ok=False, error_code="policy_refused",
                output={"reason": decision.reason},
            )

        if decision.verdict is Verdict.ESCALATE:
            ticket = await self.escalate(
                Blocker(kind="spend_approval", detail=decision.reason), context
            )
            context.audit.log_outcome(
                audit_id, "escalated", {"ticket": ticket.id, "reason": decision.reason}
            )
            return NeuronResult(
                ok=False, escalated=True, error_code="policy_escalated",
                output={"ticket": ticket.id, "reason": decision.reason},
            )

        # ALLOW — and only now does the wallet get asked to sign.
        try:
            tx = self._wallet.pay(
                to=intent.counterparty,
                amount_usdc=intent.amount_usdc,
                memo=f"{intent.category}:{intent.strategy_id or '-'}",
            )
        except Exception as exc:
            context.audit.log_outcome(audit_id, "fail", {"error": str(exc)})
            return NeuronResult(ok=False, error_code="wallet_error")

        # If a pre-approval rule cleared this spend, advance its use
        # counter so a max_uses limit can be enforced. The policy
        # decision's ``layer`` is the source of truth — it's
        # "pre_approval:<rule_id>" exactly when a rule fired. Best-effort:
        # a wallet without ``consume_preapproval`` (older / minimal
        # adapters) skips silently; the spend already succeeded.
        if decision.layer.startswith("pre_approval:"):
            rule_id = decision.layer.split(":", 1)[1]
            consumer = getattr(self._wallet, "consume_preapproval", None)
            if callable(consumer):
                try:
                    consumer(rule_id)
                except Exception:
                    pass
        context.audit.log_outcome(
            audit_id, "ok", {"tx": tx, "amount_usdc": intent.amount_usdc}
        )
        return NeuronResult(
            ok=True, cost_actual_usdc=intent.amount_usdc, output={"tx": tx}
        )
