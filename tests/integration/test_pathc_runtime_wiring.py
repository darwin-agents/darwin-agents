"""Tests for Path-C admission gate wiring into DarwinRuntime."""

from __future__ import annotations

import pytest

from darwin_dna.evolution import boot
from darwin_dna.machinery.neuron_proposal import (
    NeuronProposal,
    NeuronProposalError,
)
from darwin_fakes import make_platform


def _proposal(name="learned.summary"):
    return NeuronProposal(
        name=name,
        rationale="condense a search result into a 1-line takeaway",
        capability_tags=frozenset({"web.read"}),
        steps=("http GET https://api.search.example/summarize",),
        est_cost_usdc_p50=0.005,
        est_cost_usdc_p95=0.05,
    )


@pytest.mark.asyncio
async def test_runtime_has_admission_gate():
    _, stack = make_platform()
    runtime = boot(stack.platform())
    assert runtime.neuron_admission is not None


@pytest.mark.asyncio
async def test_admit_proposal_adds_to_catalog():
    _, stack = make_platform()
    runtime = boot(stack.platform())
    before = set(runtime.catalog)
    p = _proposal()
    runtime.neuron_admission.submit(
        p, existing_catalog=runtime.catalog,
    )
    runtime.admit_neuron_proposal(p.name)
    after = set(runtime.catalog)
    assert (after - before) == {p.name}


@pytest.mark.asyncio
async def test_admit_unknown_proposal_refused():
    _, stack = make_platform()
    runtime = boot(stack.platform())
    with pytest.raises(NeuronProposalError):
        runtime.admit_neuron_proposal("learned.never_submitted")


@pytest.mark.asyncio
async def test_admit_event_lands_in_audit():
    _, stack = make_platform()
    runtime = boot(stack.platform())
    p = _proposal()
    runtime.neuron_admission.submit(
        p, existing_catalog=runtime.catalog,
    )
    runtime.admit_neuron_proposal(p.name)
    records = stack.audit_sink.read_all()
    admitted = [
        r for r in records
        if r["kind"] == "event:neuron_proposal.admitted"
    ]
    assert len(admitted) == 1


@pytest.mark.asyncio
async def test_admission_refused_audit_when_unknown():
    _, stack = make_platform()
    runtime = boot(stack.platform())
    with pytest.raises(NeuronProposalError):
        runtime.admit_neuron_proposal("learned.ghost")
    records = stack.audit_sink.read_all()
    refused = [
        r for r in records
        if r["kind"] == "event:neuron_proposal.admission_refused"
    ]
    assert len(refused) == 1
