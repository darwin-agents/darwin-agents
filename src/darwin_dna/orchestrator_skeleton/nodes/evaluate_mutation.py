"""evaluate_mutation — empirical validation, not proof (P5, docs 03/10).

The candidate is *run*, not reasoned about. We build a frozen scenario from
the trigger context, replay baseline vs. candidate trajectories through the
in-DNA harness, and score both with the same `compute_fitness` the lineage is
selected on. Promotion requires *measured* improvement plus the invariant
gates (independent re-validation of the patch — the candidate cannot tamper
with its own evaluator). Anything else is archived as a negative lesson.

This replaces the earlier rationale-string heuristic: behaviour is measured,
never asserted.
"""

from __future__ import annotations

from ...machinery.eval_harness import (
    BehaviorDelta,
    FrozenScenario,
    evaluate_mutation_candidate,
)
from ...machinery.patch_validator import FileChange, Patch, validate_patch


def run(state, ctx):
    p = state.plan
    failure_class = p.get("failure_class", "")
    recurrence = int(p.get("recurrence", 1))
    archive = getattr(ctx, "archive", None)

    audit_id = ctx.audit.log_intent(
        "mutation.evaluate", {"failure_class": failure_class}
    )

    # Independent re-validation in this clean substrate — never trust the
    # propose step's verdict; the evaluator re-derives it.
    patch_dict = p.get("patch") or {}
    try:
        patch = Patch(
            rationale=patch_dict.get("rationale", ""),
            changes=[FileChange(**c) for c in patch_dict.get("changes", [])],
        )
        validator_clean = validate_patch(patch).ok
    except Exception:
        validator_clean = False

    try:
        delta = BehaviorDelta(**(p.get("behavior_delta") or {}))
    except Exception:
        delta = BehaviorDelta(kind="invalid", failure_class=failure_class,
                              after_failures=1)

    scenario = FrozenScenario.from_trigger(failure_class, recurrence)
    result = evaluate_mutation_candidate(
        delta, scenario, hp=ctx.hp, validator_clean=validator_clean
    )
    ctx.audit.log_outcome(
        audit_id,
        "ok",
        {"promote": result.promote, "reason": result.reason,
         "baseline_score": result.baseline.score,
         "candidate_score": result.candidate.score},
    )

    if result.promote:
        if archive is not None:
            archive.record(failure_class, "promoted", result.reason)
        state.plan = {"action": "apply_mutation", "failure_class": failure_class}
        state.route = "apply_mutation"
    else:
        if archive is not None:
            archive.record(failure_class, "rejected", result.reason)
        ctx.audit.log_event(
            "mutation.archived",
            {"failure_class": failure_class, "reason": result.reason},
        )
        state.plan = {"action": "sleep", "reason": "mutation archived"}
        state.route = "check_reproduction"
    return state
