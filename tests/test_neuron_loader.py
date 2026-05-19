"""Neuron contract enforcement + the one-money-door invariant (04)."""

from __future__ import annotations

import pytest

from darwin_dna.machinery.neuron_loader import (
    NeuronContractError,
    load_catalog,
    validate_neuron,
)
from darwin_dna.neurons._base import BaseNeuron, CostEstimate


class _Wallet:
    def balance_usdc(self): return 10.0
    def spent_today_usdc(self): return 0.0
    def spent_lifetime_usdc(self): return 0.0
    def recent_spend_usdc(self): return ()
    def pay(self, *, to, amount_usdc, memo): return {"tx": "0x1"}


class _Prov:
    def provision(self, kind, spec): return {"id": "x", "creds": "c"}


def test_founding_catalog_loads():
    from darwin_dna.neurons import build_catalog

    cat = build_catalog(wallet=_Wallet(), provision=_Prov())
    assert "payments.facilitator" in cat
    assert len(cat) == 5


def test_non_async_method_rejected():
    class Bad(BaseNeuron):
        name = "bad"
        version = "1.0.0"
        capability_tags = frozenset({"x"})
        cost_estimate = CostEstimate(
            usd_p50=0, usd_p95=0, latency_p50_ms=0, latency_p95_ms=0,
            escalation_probability=0,
        )
        failure_modes = []

        def plan(self, t, c): ...  # not async
        async def execute(self, p, c): ...

    with pytest.raises(NeuronContractError, match="async"):
        validate_neuron(Bad())


def test_only_facilitator_may_hold_treasury_write():
    class Sneaky(BaseNeuron):
        name = "content.thief"
        version = "1.0.0"
        capability_tags = frozenset({"treasury.write"})
        cost_estimate = CostEstimate(
            usd_p50=0, usd_p95=0, latency_p50_ms=0, latency_p95_ms=0,
            escalation_probability=0,
        )
        failure_modes = []

        async def plan(self, t, c): ...
        async def execute(self, p, c): ...

    with pytest.raises(NeuronContractError, match="one neuron"):
        validate_neuron(Sneaky())


def test_duplicate_names_rejected():
    from darwin_dna.neurons.content_writer import ContentWriter

    with pytest.raises(NeuronContractError, match="duplicate"):
        load_catalog([ContentWriter(), ContentWriter()])


def test_zero_treasury_writers_rejected():
    from darwin_dna.neurons.content_writer import ContentWriter

    with pytest.raises(NeuronContractError, match="exactly one treasury-write"):
        load_catalog([ContentWriter()])
