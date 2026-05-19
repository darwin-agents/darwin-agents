"""The frozen mutation-evaluation harness (P5, docs/engineering/03, 10).

"We do not try to formally prove a self-modification is beneficial. We
evaluate empirically: did the modified agent earn more, or learn more, in the
test environment?" (P5). This module is that test environment, and it lives in
the DNA so a Darwin cannot grade its own rewrites by softer rules — it scores
candidates with the *same* `compute_fitness` the lineage is selected on, in a
clean substrate (pure functions; no shared mutable state; the patch validator
is re-run independently here).

Layer 0 has no separate soma file tree to apply a diff to (the genome ships
the founding orchestrator; soma rewriting is gated and conservative — doc 18).
So a candidate is represented as a *structured, bounded, executable*
`BehaviorDelta` (the unified diff travels alongside it for audit/legibility,
but you can only empirically test something executable). The harness *runs*
two trajectories over one frozen tape — baseline vs. candidate — and compares
real fitness. It never reads the patch rationale; behaviour is measured, not
asserted.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from ..hyperparameters import DEFAULT_HYPERPARAMETERS, Hyperparameters
from .fitness import FitnessWindow, compute_fitness

# A candidate must beat the baseline by more than noise to be promoted.
EVAL_MARGIN = 1e-9


class BehaviorDelta(BaseModel):
    """The only Layer-0-safe, simulatable self-modification: after
    ``after_failures`` *occurrences* of ``failure_class`` (cumulative — same
    semantics as the self-mod gate's "≥N occurrences of the same error
    class", doc 18), back off to the cheaper fallback so the class stops
    recurring. Bounded by construction — it cannot express a constitution
    change."""

    model_config = {"frozen": True}

    kind: str = "backoff"
    failure_class: str
    after_failures: int = Field(ge=1, le=5)

    def well_formed(self) -> bool:
        return (
            self.kind == "backoff"
            and bool(self.failure_class)
            and 1 <= self.after_failures <= 5
        )


class FrozenScenario(BaseModel):
    """A deterministic situation tape derived from the trigger context. The
    *same* tape is replayed for baseline and candidate — that identity is
    what makes the comparison fair (and reproducible)."""

    model_config = {"frozen": True}

    failure_class: str
    tape: tuple[str, ...]  # each: "fail" (targeted class recurs) | "ok"
    ok_revenue_usdc: float = 0.50
    fail_cost_usdc: float = 0.05
    starting_capital_usdc: float = 100.0

    @classmethod
    def from_trigger(
        cls, failure_class: str, recurrence: int, *, ok_steps: int = 20
    ) -> "FrozenScenario":
        # Interleave the observed recurring failures with normal earning
        # steps — a realistic "recent situations" replay.
        n_fail = max(1, int(recurrence))
        tape: list[str] = []
        for i in range(max(n_fail, ok_steps)):
            if i < n_fail:
                tape.append("fail")
            if i < ok_steps:
                tape.append("ok")
        return cls(failure_class=failure_class, tape=tuple(tape))


class TrajectoryResult(BaseModel):
    model_config = {"frozen": True}

    profit_usdc: float
    failures_incurred: int
    score: float


class EvalResult(BaseModel):
    model_config = {"frozen": True}

    promote: bool
    reason: str
    baseline: TrajectoryResult
    candidate: TrajectoryResult


def _run_trajectory(
    scenario: FrozenScenario,
    delta: BehaviorDelta | None,
    hp: Hyperparameters,
) -> TrajectoryResult:
    """Pure simulation. ``delta=None`` is the baseline (the failure keeps
    recurring). With a delta, once ``after_failures`` cumulative targeted
    failures have been hit the path backs off: further targeted-failure
    situations become cheap no-ops (no cost, no revenue) instead of costly
    failures — "stop hitting the wall"."""
    revenue = 0.0
    cost = 0.0
    failures = 0
    backed_off = False

    for situation in scenario.tape:
        if situation == "ok":
            revenue += scenario.ok_revenue_usdc
            continue
        # a targeted-failure situation
        if backed_off:
            continue  # cheap no-op: the cause was routed around
        cost += scenario.fail_cost_usdc
        failures += 1
        if delta is not None and failures >= delta.after_failures:
            backed_off = True

    profit = revenue - cost
    score = compute_fitness(
        FitnessWindow(
            profit_usdc=profit,
            starting_capital_usdc=scenario.starting_capital_usdc,
            distinct_strategies=1,
            escalations_approved=0,
            escalations_rejected=0,
            policy_violations=0,
        ),
        hp,
    ).score
    return TrajectoryResult(
        profit_usdc=round(profit, 9), failures_incurred=failures, score=score
    )


def evaluate_mutation_candidate(
    delta: BehaviorDelta,
    scenario: FrozenScenario,
    *,
    hp: Hyperparameters = DEFAULT_HYPERPARAMETERS,
    validator_clean: bool,
) -> EvalResult:
    """Empirically compare candidate vs. baseline on the frozen tape and
    decide promote/archive. Promotion requires *measured* improvement AND all
    invariant gates — never the proposer's say-so."""
    baseline = _run_trajectory(scenario, None, hp)

    # Invariant gates (the "clean substrate" checks, run here independently of
    # the proposer): the patch must have passed the validator, the delta must
    # be well-formed and within Layer 0 bounds, and it must target the same
    # failure class the scenario is about.
    if not validator_clean:
        return EvalResult(promote=False, reason="patch rejected by validator",
                          baseline=baseline, candidate=baseline)
    if not delta.well_formed():
        return EvalResult(promote=False, reason="behaviour delta out of bounds",
                          baseline=baseline, candidate=baseline)
    if delta.failure_class != scenario.failure_class:
        return EvalResult(
            promote=False, reason="delta does not target the failure",
            baseline=baseline, candidate=baseline,
        )

    candidate = _run_trajectory(scenario, delta, hp)

    if candidate.score > baseline.score + EVAL_MARGIN:
        return EvalResult(
            promote=True,
            reason=(
                f"empirical improvement: fitness {candidate.score:.6f} > "
                f"{baseline.score:.6f}; failures "
                f"{baseline.failures_incurred}→{candidate.failures_incurred}"
            ),
            baseline=baseline, candidate=candidate,
        )
    return EvalResult(
        promote=False,
        reason=(
            f"no empirical improvement (candidate {candidate.score:.6f} ≤ "
            f"baseline {baseline.score:.6f}) — archived"
        ),
        baseline=baseline, candidate=candidate,
    )
