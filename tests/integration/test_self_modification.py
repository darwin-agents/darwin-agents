"""The self-modification sub-graph (docs/engineering/03, 18).

The founding strategy idles before it ever earns enough to mutate, so this
path is invisible to the normal suite. These tests drive it directly: a
flaky neuron that builds experience while reliably failing one error class,
plus focused node tests for propose → evaluate → apply and the validator gate.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace


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
    asyncio.run(propose_mutation.run(st, ctx))
    assert st.route == "evaluate_mutation"
    assert st.plan["patch"]["rationale"]


def test_propose_dna_targeting_patch_is_archived_not_applied():
    ctx = _ctx(_LLM({"rationale": "sneak into DNA",
                     "changes": [{"path": "src/darwin_dna/hyperparameters.py",
                                  "diff": "+CAP=1e9\n"}]}))
    st = OrchestratorState()
    st.plan = {"failure_class": "flaky_io"}
    asyncio.run(propose_mutation.run(st, ctx))
    assert st.route == "check_reproduction"
    kinds = [r["kind"] for r in ctx.audit._sink.read_all()]
    assert "event:mutation.archived" in kinds


def test_propose_backs_off_when_coding_tier_unavailable():
    ctx = _ctx(_LLM(raises=LLMError("coding tier daily cap exhausted")))
    st = OrchestratorState()
    st.plan = {"failure_class": "flaky_io"}
    asyncio.run(propose_mutation.run(st, ctx))  # must not raise
    assert st.route == "check_reproduction"
    assert st.plan["action"] == "sleep"


def test_evaluate_promotes_only_on_measured_improvement_not_rationale():
    # evaluate_mutation now *runs* the candidate through the in-DNA harness;
    # the rationale string is irrelevant. A backoff that empirically reduces
    # failures promotes; one that cannot (backs off after more occurrences
    # than ever happen) is archived — same patch text, opposite verdict.
    ctx = _ctx(_LLM(None))
    valid_patch = {"rationale": "anything",
                   "changes": [{"path": "soma/strategies/x.py",
                                "diff": "+    pass\n"}]}

    promote = OrchestratorState()
    promote.plan = {"failure_class": "flaky_io", "patch": valid_patch,
                    "recurrence": 10,
                    "behavior_delta": {"kind": "backoff",
                                       "failure_class": "flaky_io",
                                       "after_failures": 2}}
    evaluate_mutation.run(promote, ctx)
    assert promote.route == "apply_mutation"

    archive = OrchestratorState()
    archive.plan = {"failure_class": "flaky_io", "patch": valid_patch,
                    "recurrence": 2,  # backoff after 5 never triggers
                    "behavior_delta": {"kind": "backoff",
                                       "failure_class": "flaky_io",
                                       "after_failures": 5}}
    evaluate_mutation.run(archive, ctx)
    assert archive.route == "check_reproduction"

    # A patch the validator rejects must never reach promotion, no matter
    # what the harness would say.
    dna = OrchestratorState()
    dna.plan = {"failure_class": "flaky_io", "recurrence": 10,
                "patch": {"rationale": "x",
                          "changes": [{"path": "src/darwin_dna/x.py",
                                       "diff": "+CAP=9\n"}]},
                "behavior_delta": {"kind": "backoff",
                                   "failure_class": "flaky_io",
                                   "after_failures": 2}}
    evaluate_mutation.run(dna, ctx)
    assert dna.route == "check_reproduction"


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


# --- #16 real soma merge: evaluate forwards the patch, apply writes it -------
def test_evaluate_forwards_patch_into_apply_plan():
    """A promote verdict must carry the validated patch (+ any neuron
    proposal) into the apply plan — otherwise apply_mutation has nothing to
    merge and self-modification stays simulated."""
    ctx = _ctx(_LLM(None))
    valid_patch = {"rationale": "anything",
                   "changes": [{"path": "soma/strategies/x.py",
                                "diff": "+    pass\n"}]}
    st = OrchestratorState()
    st.plan = {"failure_class": "flaky_io", "patch": valid_patch,
               "recurrence": 10, "neuron_proposal": None,
               "behavior_delta": {"kind": "backoff",
                                  "failure_class": "flaky_io",
                                  "after_failures": 2}}
    evaluate_mutation.run(st, ctx)
    assert st.route == "apply_mutation"
    assert st.plan["patch"] == valid_patch


def _merge_hook(tmp_path):
    from darwin_platform.evolution import SomaMerger
    (tmp_path / "soma" / "strategies").mkdir(parents=True)
    (tmp_path / "soma" / "strategies" / "x.py").write_text(
        "def f():\n    return 1\n"
    )
    return SomaMerger(tmp_path)


_REAL_DIFF = (
    "--- a/soma/strategies/x.py\n+++ b/soma/strategies/x.py\n"
    "@@ -1,2 +1,3 @@\n def f():\n-    return 1\n+    # backoff\n+    return 2\n"
)


def test_apply_merges_real_patch_when_hook_wired(tmp_path):
    merger = _merge_hook(tmp_path)
    ctx = _ctx(_LLM(None))
    ctx.soma_merge = merger
    st = OrchestratorState()
    st.recurring_failures = {"flaky_io": 5}
    st.plan = {"failure_class": "flaky_io",
               "patch": {"rationale": "fix",
                         "changes": [{"path": "soma/strategies/x.py",
                                      "diff": _REAL_DIFF}]}}
    apply_mutation.run(st, ctx)
    # The real diff is on disk and the fix counts as landed.
    assert (tmp_path / "soma" / "strategies" / "x.py").read_text() == (
        "def f():\n    # backoff\n    return 2\n"
    )
    assert "flaky_io" not in st.recurring_failures
    assert st.route == "check_reproduction"


def test_apply_keeps_counter_when_merge_fails(tmp_path):
    """A merge that fails (the diff doesn't apply) must NOT clear the
    recurrence counter — the fix never reached the soma, so the loop has to
    keep trying."""
    merger = _merge_hook(tmp_path)
    ctx = _ctx(_LLM(None))
    ctx.soma_merge = merger
    st = OrchestratorState()
    st.recurring_failures = {"flaky_io": 5}
    st.plan = {"failure_class": "flaky_io",
               "patch": {"rationale": "x",
                         "changes": [{"path": "soma/strategies/x.py",
                                      "diff": "@@ -1,1 +1,1 @@\n-NOPE\n+y\n"}]}}
    apply_mutation.run(st, ctx)
    # File untouched, counter kept.
    assert (tmp_path / "soma" / "strategies" / "x.py").read_text() == (
        "def f():\n    return 1\n"
    )
    assert st.recurring_failures.get("flaky_io") == 5
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
