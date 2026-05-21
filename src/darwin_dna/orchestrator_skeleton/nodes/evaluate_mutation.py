"""evaluate_mutation — empirical validation, not proof (P5, docs 03/10/21).

The candidate is *run*, not reasoned about. We build a frozen scenario from
the trigger context, replay baseline vs. candidate trajectories through the
in-DNA harness, and score both with the same `compute_fitness` the lineage is
selected on. Promotion requires *measured* improvement plus the invariant
gates (independent re-validation of the patch — the candidate cannot tamper
with its own evaluator). Anything else is archived as a negative lesson.

Phase 1 (doc 21 §"Patch staging pipeline"): when the platform's staging
service is wired onto ``ctx.patch_staging``, the candidate is staged in a
sandbox with the *real* patch applied to a copy of the soma, and the
sandboxed harness replays N recent audit entries (not the synthetic
trigger-only tape) before returning the verdict. Absence of the hook is
the Phase-0 fallback (in-DNA harness only).

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
        patch = None
        validator_clean = False

    try:
        delta = BehaviorDelta(**(p.get("behavior_delta") or {}))
    except Exception:
        delta = BehaviorDelta(kind="invalid", failure_class=failure_class,
                              after_failures=1)

    # Phase 1: when the platform staging hook is present AND the patch is
    # well-formed, route the evaluation through the sandboxed pipeline.
    # The hook's StagingVerdict has the same promote/reason/baseline/
    # candidate score shape, so the downstream logic is identical.
    staging = getattr(ctx, "patch_staging", None) if validator_clean else None
    used_staging = False
    if staging is not None and patch is not None:
        try:
            verdict = staging.evaluate(
                patch=patch, delta=delta,
                failure_class=failure_class, recurrence=recurrence,
            )
            promote = bool(verdict.promote)
            reason = verdict.reason
            baseline_score = float(verdict.baseline_score)
            candidate_score = float(verdict.candidate_score)
            used_staging = True
        except Exception as exc:
            # A staging failure must not destabilise the loop — fall back
            # to the in-DNA harness and surface the reason on the audit.
            ctx.audit.log_event(
                "mutation.staging_error", {"reason": str(exc)[:200]},
            )
            staging = None

    if not used_staging:
        scenario = FrozenScenario.from_trigger(failure_class, recurrence)
        result = evaluate_mutation_candidate(
            delta, scenario, hp=ctx.hp, validator_clean=validator_clean
        )
        promote = result.promote
        reason = result.reason
        baseline_score = result.baseline.score
        candidate_score = result.candidate.score

    ctx.audit.log_outcome(
        audit_id,
        "ok",
        {"promote": promote, "reason": reason,
         "baseline_score": baseline_score,
         "candidate_score": candidate_score,
         "via": "staging" if used_staging else "in_dna_harness"},
    )

    if promote:
        if archive is not None:
            archive.record(failure_class, "promoted", reason)
        state.plan = {"action": "apply_mutation", "failure_class": failure_class}
        state.route = "apply_mutation"
    else:
        if archive is not None:
            archive.record(failure_class, "rejected", reason)
        ctx.audit.log_event(
            "mutation.archived",
            {"failure_class": failure_class, "reason": reason,
             "via": "staging" if used_staging else "in_dna_harness"},
        )
        state.plan = {"action": "sleep", "reason": "mutation archived"}
        state.route = "check_reproduction"
    return state
