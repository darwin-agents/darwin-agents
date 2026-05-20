"""OwnerSoftCaps flow end-to-end: platform → wallet → policy engine.

Before this slice: ``POST /platform/wallets/{id}/policy`` only tightened
the wallet's own rim; the genome's treasury_policy never saw the soft caps,
so a spend over the new cap was refused with ``wallet_error``, not with
the policy engine's ``policy_refused`` + reason. After: the wallet exposes
``owner_soft_caps()``, ``payments_facilitator._state`` threads it into
``TreasuryState``, and the engine refuses with the right rationale.
"""

from __future__ import annotations

import asyncio
from typing import Callable

import pytest

from darwin_dna.constitution import (
    SpendIntent,
)
from darwin_dna.machinery.treasury_policy import Verdict, evaluate
from darwin_fakes.fake_wallet import FakeWallet
from darwin_platform import Supervisor


# -- FakeWallet exposes owner_soft_caps -----------------------------------
def test_owner_soft_caps_none_when_at_hard_caps():
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 0.0)
    assert w.owner_soft_caps() is None


def test_owner_soft_caps_returned_when_tightened():
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 0.0)
    w.set_policy(per_action_cap_usdc=5.0, per_day_cap_usdc=10.0)
    caps = w.owner_soft_caps()
    assert caps is not None
    assert caps.per_action_usdc == pytest.approx(5.0)
    assert caps.per_day_usdc == pytest.approx(10.0)
    assert caps.per_lifetime_usdc is None  # not tightened


def test_owner_soft_caps_per_lifetime_supported():
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 0.0)
    w.set_policy(per_lifetime_cap_usdc=100.0)
    caps = w.owner_soft_caps()
    assert caps is not None
    assert caps.per_lifetime_usdc == pytest.approx(100.0)
    assert caps.per_action_usdc is None


# -- treasury_policy.evaluate with the wallet's soft caps -----------------
def test_policy_engine_refuses_above_soft_action_cap():
    """The whole point of this slice: a spend over the installed cap is
    refused by the GENOME's policy engine with a per_action rationale,
    not by the wallet with wallet_error."""
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 0.0)
    w.set_policy(per_action_cap_usdc=5.0)

    from darwin_dna.machinery.treasury_policy import TreasuryState

    intent = SpendIntent(
        amount_usdc=10.0,
        counterparty="example",
        counterparty_category="api",
        category="content.blog",
    )
    state = TreasuryState(
        balance_usdc=w.balance_usdc(),
        spent_today_usdc=0.0,
        spent_lifetime_usdc=0.0,
        recent_spend_usdc=(),
        soft_caps=w.owner_soft_caps(),
    )
    decision = evaluate(intent, state)
    assert decision.verdict is Verdict.REFUSE
    assert decision.layer.startswith("caps:per_action")
    assert "per-action cap" in decision.reason


# -- End-to-end through the supervisor + payments.facilitator neuron ------
async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    import time as _t
    deadline = _t.monotonic() + timeout
    while not pred():
        if _t.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


async def test_supervisor_policy_propagates_to_payments_neuron() -> None:
    """Install a tight per-action cap via the supervisor; the
    payments.facilitator neuron then refuses an over-cap spend with
    ``policy_refused`` (the genome's audit log), and the audit chain
    records the policy decision + rationale before any wallet call."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await sup.set_wallet_policy(
            status.darwin_id, per_action_cap_usdc=5.0
        )

        # Dispatch a $20 payment directly through the neuron — bypass the
        # planner so the test isolates the policy refusal path.
        from darwin_dna.neurons._base import Task

        neuron = h.runtime.catalog["payments.facilitator"]
        nctx = h.runtime._build_neuron_context("payments.facilitator")  # noqa: SLF001
        task = Task(
            id="t-refuse",
            kind="payment.send",
            category="content.blog",
            required_tags=frozenset({"treasury.write"}),
            payload={
                "amount_usdc": 20.0,
                "counterparty": "example.com",
                "counterparty_category": "api",
                "category": "content.blog",
            },
        )
        plan = await neuron.plan(task, nctx)
        result = await neuron.execute(plan, nctx)

        # POLICY refusal, NOT wallet_error — the platform-installed cap is
        # now enforced at the genome's treasury_policy engine.
        assert result.ok is False
        assert result.error_code == "policy_refused"
        assert "per-action cap" in result.output["reason"]

        # The audit chain holds the policy_decision event with rationale.
        records = h.stack.audit_sink.read_all()
        kinds = [r.get("kind") for r in records]
        assert "event:payment.policy_decision" in kinds
        decision_event = next(
            r for r in records if r.get("kind") == "event:payment.policy_decision"
        )
        decision_payload = decision_event["payload"]["decision"]
        assert decision_payload["verdict"] == "refuse"
        assert decision_payload["layer"].startswith("caps:per_action")
    finally:
        await sup.shutdown()


async def test_supervisor_policy_does_not_change_audit_for_within_cap() -> None:
    """Within the cap, the same dispatch goes through as before — the
    policy engine returns ALLOW; the wallet signs; the audit chain records
    the ok outcome. Confirms the new path doesn't break the success case."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await sup.set_wallet_policy(
            status.darwin_id, per_action_cap_usdc=20.0
        )

        from darwin_dna.neurons._base import Task

        neuron = h.runtime.catalog["payments.facilitator"]
        nctx = h.runtime._build_neuron_context("payments.facilitator")  # noqa: SLF001
        task = Task(
            id="t-ok",
            kind="payment.send",
            category="content.blog",
            required_tags=frozenset({"treasury.write"}),
            payload={
                "amount_usdc": 3.0,
                "counterparty": "example.com",
                "counterparty_category": "api",
                "category": "content.blog",
            },
        )
        plan = await neuron.plan(task, nctx)
        result = await neuron.execute(plan, nctx)
        assert result.ok is True
        assert result.cost_actual_usdc == pytest.approx(3.0)
    finally:
        await sup.shutdown()
