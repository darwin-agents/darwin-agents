"""The empirical mutation-eval harness (P5, docs 03/10).

Pins the property that promotion is *measured*, not asserted: the candidate
is run against a frozen tape, scored by the same DNA fitness function the
lineage is selected on, in a clean substrate. The earlier rationale-string
heuristic is gone.
"""

from __future__ import annotations

import asyncio

from darwin_dna.machinery.eval_harness import (
    BehaviorDelta,
    FrozenScenario,
    evaluate_mutation_candidate,
    evaluate_strategy_candidate,
)
from darwin_dna.strategies import StrategyAction, StrategySpec


def _delta(fc="flaky_io", after=2):
    return BehaviorDelta(kind="backoff", failure_class=fc, after_failures=after)


def _strategy(kind, sid):
    """A minimal real Strategy whose every proposal is ``kind``."""

    class _S:
        @property
        def spec(self):
            return StrategySpec(
                id=sid, archetype="content.affiliate",
                neurons_used=frozenset({"content.writer"}),
                required_capabilities=frozenset(),
                fitness_signals=["x"], est_daily_cost_usdc=0.1,
                revenue_cadence_days=7.0,
            )

        def propose(self, obs):
            return StrategyAction(
                kind=kind, reason="r",
                neuron="content.writer" if kind == "dispatch" else None,
            )

    return _S()


# -- real candidate-strategy evaluation (doc 33) --------------------------
def test_real_strategy_active_beats_idle_baseline():
    sc = FrozenScenario.from_trigger("flaky", recurrence=3)
    r = evaluate_strategy_candidate(
        _strategy("wait", "idle"), _strategy("dispatch", "active"), sc,
        validator_clean=True,
    )
    assert r.promote
    assert r.candidate.score > r.baseline.score


def test_real_strategy_inert_candidate_rejected():
    sc = FrozenScenario.from_trigger("flaky", recurrence=3)
    r = evaluate_strategy_candidate(
        _strategy("dispatch", "active"), _strategy("wait", "idle"), sc,
        validator_clean=True,
    )
    assert not r.promote
    assert "inert" in r.reason


def test_real_strategy_validator_dirty_rejected():
    sc = FrozenScenario.from_trigger("flaky", recurrence=3)
    r = evaluate_strategy_candidate(
        _strategy("wait", "idle"), _strategy("dispatch", "active"), sc,
        validator_clean=False,
    )
    assert not r.promote


def test_real_strategy_crashing_candidate_rejected():
    sc = FrozenScenario.from_trigger("flaky", recurrence=3)

    class _Boom:
        @property
        def spec(self):
            return _strategy("wait", "boom").spec

        def propose(self, obs):
            raise ValueError("boom")

    r = evaluate_strategy_candidate(
        _strategy("dispatch", "base"), _Boom(), sc, validator_clean=True,
    )
    assert not r.promote
    assert "raised" in r.reason


def test_real_strategy_eval_is_pure():
    sc = FrozenScenario.from_trigger("flaky", recurrence=5)
    a = evaluate_strategy_candidate(
        _strategy("wait", "i"), _strategy("dispatch", "a"), sc,
        validator_clean=True,
    )
    b = evaluate_strategy_candidate(
        _strategy("wait", "i"), _strategy("dispatch", "a"), sc,
        validator_clean=True,
    )
    assert a.model_dump() == b.model_dump()


def test_real_improvement_promotes():
    sc = FrozenScenario.from_trigger("flaky_io", recurrence=10)
    r = evaluate_mutation_candidate(_delta(after=2), sc, validator_clean=True)
    assert r.promote
    assert r.candidate.failures_incurred < r.baseline.failures_incurred
    assert r.candidate.score > r.baseline.score


def test_no_improvement_is_archived_same_patch():
    # Backing off after more occurrences than ever happen changes nothing.
    sc = FrozenScenario.from_trigger("flaky_io", recurrence=3)
    r = evaluate_mutation_candidate(_delta(after=5), sc, validator_clean=True)
    assert not r.promote
    assert r.candidate.score == r.baseline.score


def test_validator_dirty_never_promotes():
    sc = FrozenScenario.from_trigger("flaky_io", recurrence=10)
    r = evaluate_mutation_candidate(_delta(after=2), sc, validator_clean=False)
    assert not r.promote and "validator" in r.reason


def test_delta_must_target_the_scenario_failure():
    sc = FrozenScenario.from_trigger("flaky_io", recurrence=10)
    r = evaluate_mutation_candidate(_delta(fc="something_else"), sc,
                                    validator_clean=True)
    assert not r.promote and "target" in r.reason


def test_malformed_delta_rejected():
    sc = FrozenScenario.from_trigger("flaky_io", recurrence=10)
    bad = BehaviorDelta(kind="rewrite_constitution", failure_class="flaky_io",
                        after_failures=1)
    r = evaluate_mutation_candidate(bad, sc, validator_clean=True)
    assert not r.promote


def test_harness_is_a_clean_pure_substrate():
    # Same inputs → same verdict, no shared mutable state between runs.
    sc = FrozenScenario.from_trigger("flaky_io", recurrence=8)
    a = evaluate_mutation_candidate(_delta(after=2), sc, validator_clean=True)
    b = evaluate_mutation_candidate(_delta(after=2), sc, validator_clean=True)
    assert a.model_dump() == b.model_dump()


def test_self_mod_loop_uses_the_harness_and_archive():
    """End-to-end: the gated self-mod path runs the harness and records
    outcomes to the mutation archive."""
    from darwin_dna.neurons import Task
    from darwin_dna.neurons._base import (
        BaseNeuron,
        CostEstimate,
        NeuronResult,
        Plan,
    )
    from darwin_dna.strategies import StrategyAction, StrategySpec
    from darwin_fakes import make_platform
    from darwin_dna.evolution import boot

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
            return (NeuronResult(ok=False, error_code="flaky_io")
                    if self.calls % 4 == 0 else NeuronResult(ok=True))

    class _Strat:
        _spec = StrategySpec(id="flaky", archetype="content.affiliate",
                             neurons_used=frozenset({"probe.flaky"}),
                             required_capabilities=frozenset(),
                             fitness_signals=[], est_daily_cost_usdc=0.0,
                             revenue_cadence_days=1.0)

        @property
        def spec(self):
            return self._spec

        def propose(self, obs):
            return StrategyAction(
                kind="dispatch", neuron="probe.flaky", reason="x",
                task=Task(id="t", kind="probe",
                          required_tags=frozenset({"probe.flaky"}),
                          category="content.affiliate"))

    box = {"n": 1_700_000_000.0}
    platform, stack = make_platform(opening_usdc=5000.0, clock=lambda: box["n"])
    rt = boot(platform, clock=lambda: box["n"],
              sleep=lambda s: box.__setitem__("n", box["n"] + s))
    rt._ctx.catalog["probe.flaky"] = _Flaky()
    rt._ctx.strategy = _Strat()

    asyncio.run(rt.run(max_heartbeats=220))

    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    assert kinds.count("intent:mutation.evaluate") >= 1
    assert kinds.count("event:mutation.applied") >= 1
    assert len(rt.archive) >= 1  # outcomes recorded as negative/positive lessons
    rt.audit.verify()
    rt.ledger.verify()
    assert rt.state.alive
