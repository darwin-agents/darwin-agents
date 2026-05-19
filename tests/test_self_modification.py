"""The self-modification sub-graph (docs/engineering/03, 18).

The founding strategy idles before it ever earns enough to mutate, so this
path is invisible to the normal suite. These tests drive it directly: a
flaky neuron that builds experience while reliably failing one error class,
plus focused node tests for propose → evaluate → apply and the validator gate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from darwin_dna.evolution import boot
from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_dna.machinery.llm_router import LLMError
from darwin_dna.neurons._base import BaseNeuron, CostEstimate, NeuronResult, Plan
from darwin_dna.neurons import Task
from darwin_dna.orchestrator_skeleton.nodes import (
    apply_mutation,
    evaluate_mutation,
    propose_mutation,
)
from darwin_dna.orchestrator_skeleton.state import OrchestratorState
from darwin_dna.strategies import StrategyAction, StrategySpec
from darwin_fakes import make_platform
from darwin_fakes.fake_platform import MemoryAuditSink


# --- focused node tests ------------------------------------------------------
class _LLM:
    def __init__(self, resp=None, raises=None):
        self._resp, self._raises = resp, raises

    def complete(self, tier, request):
        if self._raises:
            raise self._raises
        return self._resp


def _ctx(llm):
    return SimpleNamespace(
        llm=llm,
        audit=AuditLogger("d", MemoryAuditSink()),
        hp=DEFAULT_HYPERPARAMETERS,
    )


def test_propose_valid_soma_patch_routes_to_evaluate():
    ctx = _ctx(_LLM({"rationale": "fix flaky_io by backing off",
                     "changes": [{"path": "soma/strategies/x.py",
                                  "diff": "+    pass\n"}]}))
    st = OrchestratorState()
    st.plan = {"failure_class": "flaky_io"}
    propose_mutation.run(st, ctx)
    assert st.route == "evaluate_mutation"
    assert st.plan["patch"]["rationale"]


def test_propose_dna_targeting_patch_is_archived_not_applied():
    ctx = _ctx(_LLM({"rationale": "sneak into DNA",
                     "changes": [{"path": "src/darwin_dna/hyperparameters.py",
                                  "diff": "+CAP=1e9\n"}]}))
    st = OrchestratorState()
    st.plan = {"failure_class": "flaky_io"}
    propose_mutation.run(st, ctx)
    assert st.route == "check_reproduction"
    kinds = [r["kind"] for r in ctx.audit._sink.read_all()]
    assert "event:mutation.archived" in kinds


def test_propose_backs_off_when_coding_tier_unavailable():
    ctx = _ctx(_LLM(raises=LLMError("coding tier daily cap exhausted")))
    st = OrchestratorState()
    st.plan = {"failure_class": "flaky_io"}
    propose_mutation.run(st, ctx)  # must not raise
    assert st.route == "check_reproduction"
    assert st.plan["action"] == "sleep"


def test_evaluate_promotes_only_when_patch_targets_the_failure():
    ctx = _ctx(_LLM(None))
    promote = OrchestratorState()
    promote.plan = {"failure_class": "flaky_io",
                    "patch": {"rationale": "address flaky_io by backing off"}}
    evaluate_mutation.run(promote, ctx)
    assert promote.route == "apply_mutation"

    archive = OrchestratorState()
    archive.plan = {"failure_class": "flaky_io",
                    "patch": {"rationale": "unrelated cleanup"}}
    evaluate_mutation.run(archive, ctx)
    assert archive.route == "check_reproduction"


def test_apply_clears_the_recurrence_and_closes_the_gate():
    ctx = _ctx(_LLM(None))
    st = OrchestratorState()
    st.recurring_failures = {"flaky_io": 5}
    st.mutation_gate_open = True
    st.plan = {"failure_class": "flaky_io"}
    apply_mutation.run(st, ctx)
    assert "flaky_io" not in st.recurring_failures
    assert st.mutation_gate_open is False
    assert st.route == "check_reproduction"


# --- end-to-end through the real loop ---------------------------------------
class _Flaky(BaseNeuron):
    name = "probe.flaky"
    version = "1.0.0"
    capability_tags = frozenset({"probe.flaky"})
    cost_estimate = CostEstimate(usd_p50=0, usd_p95=0, latency_p50_ms=0,
                                 latency_p95_ms=0, escalation_probability=0)
    failure_modes = []

    def __init__(self):
        self.calls = 0

    async def plan(self, t, c):
        return Plan(steps=["x"], est_cost=self.cost_estimate)

    async def execute(self, p, c):
        self.calls += 1
        if self.calls % 4 == 0:  # reliably the same error class
            return NeuronResult(ok=False, error_code="flaky_io")
        return NeuronResult(ok=True)


class _FlakyStrat:
    _spec = StrategySpec(id="flaky", archetype="content.affiliate",
                         neurons_used=frozenset({"probe.flaky"}),
                         required_capabilities=frozenset(), fitness_signals=[],
                         est_daily_cost_usdc=0.0, revenue_cadence_days=1.0)

    @property
    def spec(self):
        return self._spec

    def propose(self, obs):
        return StrategyAction(
            kind="dispatch", neuron="probe.flaky", reason="probe",
            task=Task(id="t", kind="probe",
                      required_tags=frozenset({"probe.flaky"}),
                      category="content.affiliate"))


def test_recurring_failure_drives_propose_evaluate_apply(vclock):
    platform, stack = make_platform(opening_usdc=5000.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    rt._ctx.catalog["probe.flaky"] = _Flaky()
    rt._ctx.strategy = _FlakyStrat()

    asyncio.run(rt.run(max_heartbeats=220))

    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    proposed = kinds.count("intent:mutation.propose")
    applied = kinds.count("event:mutation.applied")
    assert rt.state.successful_dispatches >= DEFAULT_HYPERPARAMETERS.min_dispatches_before_mutation
    assert proposed >= 1 and applied >= 1
    assert rt.state.alive  # mutation pressure must not destabilise the loop
    rt.audit.verify()
