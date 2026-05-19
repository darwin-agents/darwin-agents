"""Escalate, don't fail — pause / resume / deny / timeout (P4, docs 06/08/10).

These pin the behaviour that closes the two biggest doc gaps:
- a blocked unit of work *parks* and is *resumed* on owner approval, instead
  of being re-failed in a loop;
- the fitness counter-pressures (escalation quality, policy violations) carry
  real data, so the anti-wireheading terms are no longer inert.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from darwin_dna.evolution import boot
from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_dna.neurons import Task
from darwin_dna.neurons._base import (
    BaseNeuron,
    Blocker,
    CostEstimate,
    NeuronResult,
    Plan,
)
from darwin_dna.orchestrator_skeleton.nodes import check_reproduction
from darwin_dna.orchestrator_skeleton.state import OrchestratorState
from darwin_dna.strategies import StrategyAction, StrategySpec
from darwin_fakes import make_platform
from darwin_fakes.fake_platform import MemoryAuditSink


class _Blocker(BaseNeuron):
    """Escalates the first ``block_n`` attempts, then succeeds."""

    name = "probe.blocker"
    version = "1.0.0"
    capability_tags = frozenset({"probe.block"})
    cost_estimate = CostEstimate(usd_p50=0, usd_p95=0, latency_p50_ms=0,
                                 latency_p95_ms=0, escalation_probability=0)
    failure_modes = []

    def __init__(self, block_n: int = 1):
        self.block_n = block_n
        self.attempts = 0
        self.successes = 0

    async def plan(self, t, c):
        return Plan(steps=["x"], est_cost=self.cost_estimate)

    async def execute(self, p, c):
        self.attempts += 1
        if self.attempts <= self.block_n:
            tk = await self.escalate(Blocker(kind="captcha", detail="wall"), c)
            return NeuronResult(ok=False, escalated=True, error_code="captcha",
                                output={"ticket": tk.id})
        self.successes += 1
        return NeuronResult(ok=True)


class _BlockStrat:
    _spec = StrategySpec(id="blk", archetype="content.affiliate",
                         neurons_used=frozenset({"probe.blocker"}),
                         required_capabilities=frozenset(), fitness_signals=[],
                         est_daily_cost_usdc=0.0, revenue_cadence_days=1.0)

    @property
    def spec(self):
        return self._spec

    def propose(self, obs):
        return StrategyAction(
            kind="dispatch", neuron="probe.blocker", reason="probe",
            task=Task(id="t", kind="probe",
                      required_tags=frozenset({"probe.block"}),
                      category="content.affiliate"))


def _wire(rt, neuron):
    rt._ctx.catalog[neuron.name] = neuron
    rt._ctx.strategy = _BlockStrat()


def test_block_parks_the_task_it_does_not_refail_in_a_loop(vclock):
    # No owner responder: the task must park, not be retried every heartbeat.
    platform, stack = make_platform(opening_usdc=100.0, clock=vclock,
                                    escalation_auto=None)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    n = _Blocker(block_n=99)
    _wire(rt, n)

    asyncio.run(rt.run(max_heartbeats=8))

    assert n.attempts == 1  # tried once, then parked — NOT 8
    assert len(rt.state.open_escalations) == 1
    assert rt.state.alive
    rt.audit.verify()


def test_owner_approval_resumes_the_exact_task(vclock):
    platform, stack = make_platform(opening_usdc=100.0, clock=vclock,
                                    escalation_auto=None)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    n = _Blocker(block_n=1)
    _wire(rt, n)

    asyncio.run(rt.run(max_heartbeats=3))
    assert n.attempts == 1 and n.successes == 0
    ticket = next(iter(rt.state.open_escalations))

    stack.escalation.respond(ticket, "approve")
    asyncio.run(rt.run(max_heartbeats=3))

    assert n.successes >= 1  # the parked task was resumed and completed
    assert rt.state.escalations_approved == 1  # exactly the one we approved
    assert rt.state.escalations_rejected == 0
    assert not rt.state.open_escalations  # closed for good


def test_denied_escalation_counts_as_rejected(vclock):
    platform, stack = make_platform(opening_usdc=100.0, clock=vclock,
                                    escalation_auto=None)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    n = _Blocker(block_n=99)
    _wire(rt, n)

    asyncio.run(rt.run(max_heartbeats=2))
    ticket = next(iter(rt.state.open_escalations))
    stack.escalation.respond(ticket, "deny")
    asyncio.run(rt.run(max_heartbeats=2))

    assert rt.state.escalations_rejected >= 1
    assert rt.state.escalations_approved == 0


def test_timeout_is_a_deterministic_deny_never_a_hang(vclock):
    platform, stack = make_platform(opening_usdc=100.0, clock=vclock,
                                    escalation_auto=None)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)
    n = _Blocker(block_n=99)
    _wire(rt, n)

    asyncio.run(rt.run(max_heartbeats=2))
    assert len(rt.state.open_escalations) == 1
    vclock.now += 16 * 60  # past the 15-min captcha deadline

    asyncio.run(rt.run(max_heartbeats=2))
    assert rt.state.escalations_rejected >= 1  # auto-denied, not stuck
    assert rt.state.alive


def test_policy_refused_payment_increments_policy_violations(vclock):
    platform, stack = make_platform(opening_usdc=100.0, clock=vclock)
    rt = boot(platform, clock=vclock, sleep=vclock.sleep)

    class _PayStrat:
        _spec = StrategySpec(id="pay", archetype="content.affiliate",
                             neurons_used=frozenset({"payments.facilitator"}),
                             required_capabilities=frozenset(),
                             fitness_signals=[], est_daily_cost_usdc=0.0,
                             revenue_cadence_days=1.0)

        @property
        def spec(self):
            return self._spec

        def propose(self, obs):
            # Over the per-action hard cap → treasury policy REFUSE.
            return StrategyAction(
                kind="dispatch", neuron="payments.facilitator", reason="x",
                task=Task(id="p", kind="payment",
                          required_tags=frozenset({"treasury.write"}),
                          category="content.affiliate",
                          payload={"amount_usdc": 9_999.0,
                                   "counterparty": "x", "counterparty_category": "api"}))

    rt._ctx.strategy = _PayStrat()
    asyncio.run(rt.run(max_heartbeats=3))
    assert rt.state.policy_violations >= 1


def test_fitness_counter_pressures_are_live():
    """check_reproduction must feed real escalation/policy data into fitness —
    a rejected escalation and a policy violation must lower the score."""
    ctx = SimpleNamespace(hp=DEFAULT_HYPERPARAMETERS,
                          audit=AuditLogger("d", MemoryAuditSink()))

    def score(**kw):
        s = OrchestratorState()
        s.starting_capital_usdc = 100.0
        s.profit_baseline_usdc = 100.0
        s.balance_usdc = 110.0  # +$10 profit
        for k, v in kw.items():
            setattr(s, k, v)
        check_reproduction.run(s, ctx)
        return s.fitness

    clean = score()
    pestered = score(escalations_rejected=3)
    violated = score(policy_violations=1)

    assert pestered < clean  # pestering the owner hurts
    assert violated < clean  # policy violation hurts, heavily
    assert clean - violated >= DEFAULT_HYPERPARAMETERS.fitness_policy_violation_penalty - 1e-9
