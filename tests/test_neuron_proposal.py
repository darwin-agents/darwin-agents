"""Tests for Path-C: Darwin-generated neurons (doc 04 phase 3)."""

from __future__ import annotations

import types

import pytest

from darwin_dna.machinery.neuron_proposal import (
    InterpretedNeuron,
    NeuronAdmissionGate,
    NeuronProposal,
    NeuronProposalError,
    PATH_C_ALLOWED_TAGS,
    validate_proposal,
)
from darwin_dna.neurons import Task
from darwin_dna.neurons._base import NeuronContext


def _proposal(
    *,
    name="learned.summary",
    rationale="condense a search result into a 1-line takeaway",
    tags=("web.read",),
    steps=("http GET https://api.search.example/summarize",),
    p50=0.005,
    p95=0.05,
) -> NeuronProposal:
    return NeuronProposal(
        name=name,
        rationale=rationale,
        capability_tags=frozenset(tags),
        steps=tuple(steps),
        est_cost_usdc_p50=p50,
        est_cost_usdc_p95=p95,
    )


# ---------------------------------------------------------------------------
# validate_proposal
# ---------------------------------------------------------------------------

def test_validates_ok_proposal():
    validate_proposal(_proposal(), existing_catalog={})


def test_refuses_name_collision():
    with pytest.raises(NeuronProposalError):
        validate_proposal(_proposal(), existing_catalog={"learned.summary": object()})


def test_refuses_non_learned_prefix():
    with pytest.raises(NeuronProposalError, match="learned."):
        validate_proposal(
            _proposal(name="content.thief"), existing_catalog={},
        )


def test_refuses_forbidden_tag():
    with pytest.raises(NeuronProposalError, match="forbidden"):
        validate_proposal(
            _proposal(tags=("treasury.write",)), existing_catalog={},
        )


def test_refuses_empty_tags():
    with pytest.raises(NeuronProposalError):
        validate_proposal(_proposal(tags=()), existing_catalog={})


def test_refuses_too_many_steps():
    with pytest.raises(NeuronProposalError, match="too many"):
        validate_proposal(
            _proposal(steps=tuple(f"sandbox python pass{i}" for i in range(10))),
            existing_catalog={},
        )


def test_refuses_oversized_p95():
    with pytest.raises(NeuronProposalError):
        validate_proposal(
            _proposal(p95=10.0), existing_catalog={},
        )


def test_refuses_p50_above_p95():
    with pytest.raises(NeuronProposalError):
        validate_proposal(
            _proposal(p50=0.1, p95=0.05), existing_catalog={},
        )


def test_allowed_tags_cover_safe_set():
    """Spot-check: treasury.write and constitution.edit must NOT be on
    the allow-list."""
    assert "treasury.write" not in PATH_C_ALLOWED_TAGS
    assert "constitution.edit" not in PATH_C_ALLOWED_TAGS


# ---------------------------------------------------------------------------
# InterpretedNeuron — step dispatch
# ---------------------------------------------------------------------------

class _Audit:
    def __init__(self): self.events = []
    def log_intent(self, k, p): self.events.append(("intent", k, p)); return "i-1"
    def log_outcome(self, i, s, p): self.events.append(("outcome", s, p))
    def log_event(self, k, p): self.events.append(("event", k, p))


def _ctx(*, http=None, sandbox=None):
    ctx = NeuronContext(
        darwin_id="d-1", llm=object(), audit=_Audit(),
        http=http, sandbox=sandbox,
    )
    return ctx


class _HTTPStub:
    def __init__(self, *, statuses):
        self.statuses = list(statuses)
        self.calls = []
    def request(self, method, url, **kw):
        self.calls.append((method, url))
        s = self.statuses.pop(0) if self.statuses else 200
        return types.SimpleNamespace(status_code=s, text="")


class _SandboxStub:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []
    def run_code(self, *, language, code, timeout_seconds=30.0):
        self.calls.append({"language": language, "code": code})
        return self.outcomes.pop(0) if self.outcomes else {
            "ok": True, "exit_code": 0, "stdout": "", "stderr": "",
        }


@pytest.mark.asyncio
async def test_http_step_success():
    n = InterpretedNeuron(_proposal(
        steps=("http GET https://x.example/", "http POST https://y.example/"),
    ))
    http = _HTTPStub(statuses=[200, 201])
    ctx = _ctx(http=http)
    task = Task(id="t", kind="x", payload={})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert len(http.calls) == 2


@pytest.mark.asyncio
async def test_http_step_failure_terminates():
    n = InterpretedNeuron(_proposal(
        steps=(
            "http GET https://x.example/",
            "http GET https://y.example/",
        ),
    ))
    http = _HTTPStub(statuses=[200, 500])
    ctx = _ctx(http=http)
    task = Task(id="t", kind="x", payload={})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "interpreted_step_failed"
    # All steps still recorded in output up to + including the failure.
    assert len(result.output["results"]) == 2


@pytest.mark.asyncio
async def test_sandbox_step_runs_code():
    n = InterpretedNeuron(_proposal(
        tags=("compute.run",),
        steps=("sandbox python print('hi')",),
    ))
    sb = _SandboxStub([{"ok": True, "exit_code": 0,
                        "stdout": "hi\n", "stderr": ""}])
    ctx = _ctx(sandbox=sb)
    task = Task(id="t", kind="x", payload={})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is True
    assert sb.calls[0]["language"] == "python"


@pytest.mark.asyncio
async def test_step_without_substrate_reports_missing_substrate():
    n = InterpretedNeuron(_proposal(
        tags=("compute.run",),
        steps=("sandbox python pass",),
    ))
    ctx = _ctx(sandbox=None)
    task = Task(id="t", kind="x", payload={})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False
    assert result.error_code == "missing_substrate"


@pytest.mark.asyncio
async def test_unknown_step_prefix_fails():
    n = InterpretedNeuron(_proposal(steps=("delete everything",)))
    ctx = _ctx()
    task = Task(id="t", kind="x", payload={})
    plan = await n.plan(task, ctx)
    result = await n.execute(plan, ctx)
    assert result.ok is False


# ---------------------------------------------------------------------------
# Admission gate
# ---------------------------------------------------------------------------

def test_admission_pipeline():
    gate = NeuronAdmissionGate()
    p = _proposal()
    gate.submit(p, existing_catalog={})
    assert len(gate.pending()) == 1
    assert len(gate.admitted()) == 0
    n = gate.admit(p.name)
    assert isinstance(n, InterpretedNeuron)
    assert p.name in gate.admitted()
    assert len(gate.pending()) == 0


def test_admission_refuses_unknown_name():
    gate = NeuronAdmissionGate()
    with pytest.raises(NeuronProposalError):
        gate.admit("learned.nope")


def test_admission_cap_enforced():
    gate = NeuronAdmissionGate(max_admitted=2)
    for i in range(3):
        gate.submit(_proposal(name=f"learned.s{i}"), existing_catalog={})
    gate.admit("learned.s0")
    gate.admit("learned.s1")
    with pytest.raises(NeuronProposalError):
        gate.admit("learned.s2")


def test_revoke_removes_admitted():
    gate = NeuronAdmissionGate()
    p = _proposal()
    gate.submit(p, existing_catalog={})
    gate.admit(p.name)
    gate.revoke(p.name)
    assert p.name not in gate.admitted()


def test_double_admit_refused():
    gate = NeuronAdmissionGate()
    p = _proposal()
    gate.submit(p, existing_catalog={})
    gate.admit(p.name)
    # Re-submitting an already-admitted name: validate_proposal
    # would pass (catalog doesn't list it), but the gate refuses.
    with pytest.raises(NeuronProposalError):
        gate.submit(_proposal(), existing_catalog={})


def test_admitted_neuron_satisfies_protocol():
    """The interpreted neuron must satisfy the genome's Neuron Protocol
    so the catalog loader admits it."""
    from darwin_dna.neurons._base import Neuron

    n = InterpretedNeuron(_proposal())
    assert isinstance(n, Neuron)
