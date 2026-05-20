"""Regression tests for P7/P8 gaps found while iterating.

These pin two guarantees the happy-path suite did not exercise:

* P8 — a kill that arrives *during* a neuron's cheap ``plan()`` stops the
  side-effecting ``execute()`` from running at all (not at the next node
  boundary, which would be too late).
* P7 — "no log, no action": if the audit logger fails mid-dispatch the Darwin
  HALTS; the failure is never swallowed as a recoverable neuron crash.
"""

from __future__ import annotations

import asyncio

import pytest

from darwin_dna.machinery.audit_logger import AuditError
from darwin_dna.neurons._base import BaseNeuron, CostEstimate, NeuronResult, Plan
from darwin_dna.strategies import StrategyAction, StrategySpec


class _StubStrategy:
    """Always dispatch to a named neuron — lets a test steer dispatch."""

    def __init__(self, neuron_name: str):
        from darwin_dna.neurons import Task

        self._spec = StrategySpec(
            id="stub", archetype="content.affiliate",
            neurons_used=frozenset({neuron_name}),
            required_capabilities=frozenset(),
            fitness_signals=[], est_daily_cost_usdc=0.0,
            revenue_cadence_days=1.0,
        )
        self._task = Task(id="t", kind="probe",
                          required_tags=frozenset({"probe.cap"}),
                          category="content.affiliate")
        self._n = neuron_name

    @property
    def spec(self):
        return self._spec

    def propose(self, obs):
        return StrategyAction(kind="dispatch", neuron=self._n,
                              reason="probe", task=self._task)


class _KillOnPlanNeuron(BaseNeuron):
    name = "probe.kill_on_plan"
    version = "1.0.0"
    capability_tags = frozenset({"probe.cap"})
    cost_estimate = CostEstimate(usd_p50=0, usd_p95=0, latency_p50_ms=0,
                                 latency_p95_ms=0, escalation_probability=0)
    failure_modes = []

    def __init__(self, kill_flag):
        self._kill = kill_flag
        self.executed = False

    async def plan(self, task, context) -> Plan:
        self._kill.flip("killed during plan()")  # kill arrives mid-dispatch
        return Plan(steps=["x"], est_cost=self.cost_estimate)

    async def execute(self, plan, context) -> NeuronResult:
        self.executed = True  # MUST NOT happen — kill polled before this
        return NeuronResult(ok=True)


def test_kill_during_plan_blocks_execute(booted):
    rt, stack = booted
    probe = _KillOnPlanNeuron(stack.kill)
    # The graph reads the context, not runtime attributes — steer it there.
    rt._ctx.catalog[probe.name] = probe
    rt._ctx.strategy = _StubStrategy(probe.name)

    asyncio.run(rt.run(max_heartbeats=5))

    assert probe.executed is False  # side effect never fired
    assert rt.state.halt is True
    assert "killed" in (rt.state.death_reason or "")
    kinds = [r["kind"] for r in stack.audit_sink.read_all()]
    assert "event:kill.halt" in kinds


class _FailAfter:
    """Audit sink that works for the first ``n`` appends then fails — models
    the disk/DB going away mid-life."""

    def __init__(self, inner, n):
        self._inner = inner
        self._left = n

    def append(self, record):
        if self._left <= 0:
            raise RuntimeError("audit storage gone")
        self._left -= 1
        self._inner.append(record)

    def read_all(self):
        return self._inner.read_all()


def test_audit_failure_halts_the_darwin_not_swallowed(vclock):
    from darwin_dna.evolution import boot
    from darwin_fakes import make_platform

    platform, stack = make_platform(opening_usdc=60.0, clock=vclock)
    # Let boot + a couple of heartbeats' worth of records through, then the
    # sink dies. The next log_intent inside a dispatch must raise AuditError
    # and that must propagate out of run() (Darwin halts) — not be caught as
    # a neuron crash and logged-and-continued.
    platform.audit_sink = _FailAfter(stack.audit_sink, n=12)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)

    with pytest.raises(AuditError):
        asyncio.run(rt.run(max_heartbeats=30))
