"""Tests for Path-C wiring through propose_mutation + apply_mutation."""

from __future__ import annotations

import pytest

from darwin_dna.evolution import boot
from darwin_dna.hyperparameters import Hyperparameters
from darwin_dna.orchestrator_skeleton.nodes.propose_mutation import (
    _validate_and_normalise_proposal,
)
from darwin_fakes import make_platform

# Path-C is OFF by default (doc 04). Tests that exercise admission must opt in.
_PATHC_AUTO = Hyperparameters(
    path_c_enabled=True, path_c_require_human_review=False,
)
_PATHC_REVIEW = Hyperparameters(
    path_c_enabled=True, path_c_require_human_review=True,
)


class _Audit:
    def __init__(self): self.events = []
    def log_intent(self, k, p): self.events.append(("intent", k, p)); return "i-1"
    def log_outcome(self, i, s, p): self.events.append(("outcome", s, p))
    def log_event(self, k, p): self.events.append(("event", k, p))


class _Ctx:
    def __init__(self, catalog=None):
        self.catalog = catalog or {}
        self.audit = _Audit()


def test_validate_normalise_valid_proposal():
    ctx = _Ctx()
    payload = {
        "name": "learned.summarize",
        "rationale": "condense search results into a one-sentence takeaway",
        "capability_tags": ["web.read"],
        "steps": ["http GET https://api.search.example/"],
        "est_cost_usdc_p50": 0.005,
        "est_cost_usdc_p95": 0.05,
    }
    out = _validate_and_normalise_proposal(payload, ctx)
    assert out is not None
    assert out["name"] == "learned.summarize"


def test_validate_normalise_drops_bad_name():
    ctx = _Ctx()
    out = _validate_and_normalise_proposal(
        {
            "name": "payments.facilitator_x",  # bad name prefix
            "rationale": "appears legitimate at first glance",
            "capability_tags": ["web.read"],
            "steps": ["http GET https://x/"],
            "est_cost_usdc_p50": 0.001, "est_cost_usdc_p95": 0.01,
        },
        ctx,
    )
    assert out is None
    refused = [
        e for e in ctx.audit.events
        if e[0] == "event" and "refused" in e[1]
    ]
    assert len(refused) == 1


def test_validate_normalise_drops_malformed():
    ctx = _Ctx()
    out = _validate_and_normalise_proposal(
        {"name": "x", "rationale": "y"},  # missing required fields
        ctx,
    )
    assert out is None


def test_validate_normalise_non_dict_input_returns_none():
    ctx = _Ctx()
    assert _validate_and_normalise_proposal("not a dict", ctx) is None
    assert _validate_and_normalise_proposal(None, ctx) is None


@pytest.mark.asyncio
async def test_runtime_context_carries_admission_gate():
    """The OrchestratorContext exposes the admission gate so
    apply_mutation can submit + admit proposals."""
    _, stack = make_platform()
    runtime = boot(stack.platform(), hp=_PATHC_AUTO)
    assert runtime.neuron_admission is not None  # Path-C enabled → real gate
    assert runtime._ctx.neuron_admission is runtime.neuron_admission
    # Bound methods compare by callable equality, not identity.
    assert (
        runtime._ctx.admit_neuron_proposal_helper.__func__
        is runtime.admit_neuron_proposal.__func__
    )


@pytest.mark.asyncio
async def test_apply_mutation_admits_proposal_when_present():
    """With Path-C enabled and human review OFF (auto-admit opt-in), a
    proposal in state.plan submits + admits via the runtime helper, growing
    the catalog by one neuron."""
    from darwin_dna.orchestrator_skeleton.nodes.apply_mutation import run

    _, stack = make_platform()
    runtime = boot(stack.platform(), hp=_PATHC_AUTO)
    pre_size = len(runtime.catalog)

    proposal = {
        "name": "learned.greet",
        "rationale": "respond to support tickets that are clearly greetings",
        "capability_tags": ["web.read"],
        "steps": ["http GET https://api.support.example/greet"],
        "est_cost_usdc_p50": 0.001,
        "est_cost_usdc_p95": 0.01,
    }

    class _State:
        recurring_failures = {"x": 5}
        mutation_gate_open = True
        plan = {
            "failure_class": "x",
            "neuron_proposal": proposal,
        }
        route = ""

    state = _State()
    run(state, runtime._ctx)
    # Catalog grew.
    assert len(runtime.catalog) == pre_size + 1
    assert "learned.greet" in runtime.catalog


@pytest.mark.asyncio
async def test_apply_mutation_skips_invalid_proposal():
    """A malformed proposal in state.plan must not block apply_mutation;
    the behavior_delta still applies + the catalog is unchanged."""
    from darwin_dna.orchestrator_skeleton.nodes.apply_mutation import run

    _, stack = make_platform()
    runtime = boot(stack.platform(), hp=_PATHC_AUTO)
    pre_size = len(runtime.catalog)
    bad_proposal = {
        "name": "payments.facilitator_evil",  # impersonation name
        "rationale": "appears legit at first glance",
        "capability_tags": ["treasury.write"],  # forbidden tag
        "steps": ["http GET https://x/"],
        "est_cost_usdc_p50": 0.001,
        "est_cost_usdc_p95": 0.01,
    }

    class _State:
        recurring_failures = {"x": 5}
        mutation_gate_open = True
        plan = {"failure_class": "x", "neuron_proposal": bad_proposal}
        route = ""

    state = _State()
    run(state, runtime._ctx)
    # Catalog unchanged.
    assert len(runtime.catalog) == pre_size


def _valid_proposal():
    return {
        "name": "learned.greet",
        "rationale": "respond to greeting tickets",
        "capability_tags": ["web.read"],
        "steps": ["http GET https://api.support.example/greet"],
        "est_cost_usdc_p50": 0.001,
        "est_cost_usdc_p95": 0.01,
    }


class _ProposalState:
    recurring_failures = {"x": 5}
    mutation_gate_open = True
    route = ""

    def __init__(self, proposal):
        self.recurring_failures = {"x": 5}
        self.plan = {"failure_class": "x", "neuron_proposal": proposal}


@pytest.mark.asyncio
async def test_path_c_disabled_by_default_never_admits():
    """doc 04: Path-C is OFF by default. The admission gate is None and a
    valid proposal is NOT admitted (and the catalog does not grow)."""
    from darwin_dna.orchestrator_skeleton.nodes.apply_mutation import run

    _, stack = make_platform()
    runtime = boot(stack.platform())  # default hp → Path-C off
    assert runtime.neuron_admission is None
    pre_size = len(runtime.catalog)
    run(_ProposalState(_valid_proposal()), runtime._ctx)
    assert len(runtime.catalog) == pre_size


@pytest.mark.asyncio
async def test_path_c_human_review_stages_without_admitting():
    """With Path-C enabled but human review ON (the default when enabled), a
    valid proposal is SUBMITTED to the staging quarantine and a review
    escalation is filed — but it is NOT merged into the live catalog until an
    operator approves."""
    from darwin_dna.orchestrator_skeleton.nodes.apply_mutation import run

    _, stack = make_platform()
    runtime = boot(stack.platform(), hp=_PATHC_REVIEW)
    pre_size = len(runtime.catalog)
    run(_ProposalState(_valid_proposal()), runtime._ctx)
    # Not admitted to the live catalog...
    assert len(runtime.catalog) == pre_size
    assert "learned.greet" not in runtime.catalog
    # ...but staged in the gate, awaiting operator approval.
    pending = [p.name for p in runtime.neuron_admission.pending()]
    assert "learned.greet" in pending
    # An operator can then approve it via the existing admit helper.
    runtime.admit_neuron_proposal("learned.greet")
    assert "learned.greet" in runtime.catalog
