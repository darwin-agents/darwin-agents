"""End-to-end Phase 1 evolution test (docs/engineering/21).

Drives one Darwin through:

  spawn → flaky workload accumulates → propose → patch staging → apply
        → (optional) reproduction → child boot

and asserts the Phase-1 acceptance criteria. The runner is
deterministic-ish: it uses the same ``_Flaky`` probe-neuron the
integration suite drives self-modification with, so the harness has a
reliable, repeating failure class to trigger the cycle.

Run::

    uv run python -m tools.evolution_test.run_evolution
    uv run python -m tools.evolution_test.run_evolution --heartbeats 400 --require-reproduction
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field

from darwin_dna.evolution import boot
from darwin_dna.hyperparameters import DEFAULT_HYPERPARAMETERS
from darwin_dna.neurons import Task
from darwin_dna.neurons._base import (
    BaseNeuron,
    CostEstimate,
    NeuronResult,
    Plan,
)
from darwin_dna.strategies import StrategyAction, StrategySpec
from darwin_fakes import make_platform
from darwin_platform.evolution.patch_staging import PatchStagingService
from darwin_platform.runtime.supervisor import Supervisor

from .assertions import (
    CriterionResult,
    assert_audit_chain_unbroken,
    assert_eval_verdict_recorded,
    assert_lineage_two_generations,
    assert_reproduction_occurred,
    assert_revenue_observed,
    assert_self_modification_occurred,
)


# A neuron that reliably fails one error class — same pattern
# tests/integration/test_self_modification.py uses. Triggering the
# mutation pressure deterministically is the whole point of this harness.
class _Flaky(BaseNeuron):
    name = "probe.flaky"
    version = "1.0.0"
    capability_tags = frozenset({"probe.flaky"})
    cost_estimate = CostEstimate(
        usd_p50=0.0, usd_p95=0.0, latency_p50_ms=0,
        latency_p95_ms=0, escalation_probability=0.0,
    )
    failure_modes: list = []

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    async def plan(self, task, ctx):
        return Plan(steps=["x"], est_cost=self.cost_estimate)

    async def execute(self, plan, ctx):
        self.calls += 1
        if self.calls % 4 == 0:
            return NeuronResult(ok=False, error_code="flaky_io")
        return NeuronResult(ok=True)


class _FlakyStrategy:
    _spec = StrategySpec(
        id="flaky", archetype="content.affiliate",
        neurons_used=frozenset({"probe.flaky"}),
        required_capabilities=frozenset(), fitness_signals=[],
        est_daily_cost_usdc=0.0, revenue_cadence_days=1.0,
    )

    @property
    def spec(self):
        return self._spec

    def propose(self, obs):
        return StrategyAction(
            kind="dispatch", neuron="probe.flaky", reason="probe",
            task=Task(
                id="t", kind="probe",
                required_tags=frozenset({"probe.flaky"}),
                category="content.affiliate",
            ),
        )


@dataclass
class EvolutionReport:
    heartbeats: int
    propose_count: int
    apply_count: int
    reproduce_count: int
    children_count: int
    criteria: list[CriterionResult] = field(default_factory=list)
    started_at: float = 0.0
    ended_at: float = 0.0

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.criteria)

    def to_dict(self) -> dict:
        return {
            "heartbeats": self.heartbeats,
            "propose_count": self.propose_count,
            "apply_count": self.apply_count,
            "reproduce_count": self.reproduce_count,
            "children_count": self.children_count,
            "elapsed_s": round(self.ended_at - self.started_at, 3),
            "criteria": [
                {"name": c.name, "ok": c.ok, "detail": c.detail}
                for c in self.criteria
            ],
            "ok": self.ok,
        }


async def run_evolution(
    *,
    heartbeats: int = 220,
    opening_usdc: float = 5000.0,
    require_reproduction: bool = False,
    require_revenue: bool = False,
    staging_in_sandbox: bool = False,
) -> EvolutionReport:
    """Spawn a Darwin in-process, drive ``heartbeats`` heartbeats with
    the flaky probe wired in, then run the assertions.

    ``staging_in_sandbox=False`` (default) uses the in-process staging
    fallback — fast, deterministic, no external deps. Setting this to
    True would require a real sandbox factory wired on the supervisor.
    """
    started = time.time()

    # Build a supervisor configured for the in-process staging path.
    supervisor = Supervisor(
        heartbeat_seconds=0.001,
        # Patch-staging quota is the platform's pressure valve; here it
        # exists to exercise the wrapping code path even when we evaluate
        # in-process (no sandbox factory ⇒ in-process eval).
    )

    # Spawn directly via the supervisor so the Phase-1 staging hook is
    # installed on the genome's ctx by ``_install_patch_staging_hook``.
    status = await supervisor.spawn(
        opening_usdc=opening_usdc,
        actor="evolution_test",
    )
    handle = supervisor._darwins[status.darwin_id]  # noqa: SLF001
    rt = handle.runtime

    # Inject the flaky probe so the recurring-failure pressure builds.
    rt.catalog["probe.flaky"] = _Flaky()
    rt._ctx.catalog["probe.flaky"] = rt.catalog["probe.flaky"]  # noqa: SLF001
    rt._ctx.strategy = _FlakyStrategy()  # noqa: SLF001

    try:
        await rt.run(max_heartbeats=heartbeats)
    finally:
        ended = time.time()

    audit_rows = handle.stack.audit_sink.read_all()
    audit_kinds = [r.get("kind", "") for r in audit_rows]

    propose_count = sum(1 for k in audit_kinds if k == "intent:mutation.propose")
    apply_count = sum(1 for k in audit_kinds if k == "event:mutation.applied")
    reproduce_count = sum(
        1 for k in audit_kinds if k == "intent:reproduction.begin"
    )
    lineage = await supervisor.lineage_tree(status.darwin_id)
    children_count = len(lineage.get("descendants", []))

    criteria: list[CriterionResult] = []
    criteria.append(assert_self_modification_occurred(audit_rows))
    criteria.append(assert_eval_verdict_recorded(audit_rows))
    criteria.append(assert_reproduction_occurred(
        audit_rows, required=require_reproduction,
    ))
    criteria.append(assert_lineage_two_generations(
        lineage, required=require_reproduction,
    ))
    criteria.append(assert_audit_chain_unbroken(rt.audit))
    criteria.append(assert_revenue_observed(
        rt.state, required=require_revenue,
    ))

    await supervisor.shutdown(kill_running=True)

    return EvolutionReport(
        heartbeats=heartbeats,
        propose_count=propose_count,
        apply_count=apply_count,
        reproduce_count=reproduce_count,
        children_count=children_count,
        criteria=criteria,
        started_at=started,
        ended_at=ended,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--heartbeats", type=int, default=220,
                   help="Max heartbeats to drive before asserting.")
    p.add_argument("--opening-usdc", type=float, default=5000.0)
    p.add_argument("--require-reproduction", action="store_true",
                   help="Fail if no reproduction event occurred.")
    p.add_argument("--require-revenue", action="store_true",
                   help="Fail if no revenue (≥ $0.01) was measured.")
    p.add_argument("--json", action="store_true",
                   help="Emit a JSON report on stdout (machine-readable).")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    report = asyncio.run(run_evolution(
        heartbeats=args.heartbeats,
        opening_usdc=args.opening_usdc,
        require_reproduction=args.require_reproduction,
        require_revenue=args.require_revenue,
    ))
    if args.json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        print("=== Phase 1 evolution test ===")
        print(
            f"heartbeats={report.heartbeats}  "
            f"propose={report.propose_count}  apply={report.apply_count}  "
            f"reproduce={report.reproduce_count}  "
            f"children={report.children_count}  "
            f"elapsed={report.ended_at - report.started_at:.2f}s"
        )
        for c in report.criteria:
            marker = "PASS" if c.ok else "FAIL"
            print(f"  [{marker}] {c.name}: {c.detail}")
        print(f"\noverall: {'PASS' if report.ok else 'FAIL'}")
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
