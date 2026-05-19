"""propose_mutation — the gated self-modification entry (docs/engineering/03).

A coding-tier model is asked for a patch (a unified diff) targeting the
recurring failure. The patch is run through the in-DNA patch validator
*before* anything else — a patch that touches the genome, the validator, the
kill-switch poll, the audit calls, or the treasury enforcement points is
rejected here and archived as a negative lesson. The Darwin cannot rewrite the
thing that judges its rewrites.
"""

from __future__ import annotations

from ...machinery.llm_router import LLMError, Tier
from ...machinery.patch_validator import FileChange, Patch, validate_patch


def run(state, ctx):
    failure_class = state.plan.get("failure_class", "")
    audit_id = ctx.audit.log_intent(
        "mutation.propose", {"failure_class": failure_class}
    )

    try:
        resp = ctx.llm.complete(
            Tier.CODING,
            {
                "task": "propose_patch",
                "failure_class": failure_class,
                "guidance": "fix the recurring failure; soma files only",
            },
        )
    except LLMError as exc:
        # Coding tier is hard-capped and expensive; a miss is normal, not a
        # crash. Back off and keep earning.
        ctx.audit.log_outcome(audit_id, "fail", {"error": str(exc)})
        state.plan = {"action": "sleep", "reason": "coding tier unavailable"}
        state.route = "check_reproduction"
        return state

    patch = Patch(
        rationale=resp.get("rationale", f"address {failure_class}"),
        changes=[
            FileChange(path=c["path"], diff=c["diff"])
            for c in resp.get("changes", [])
        ]
        or [
            FileChange(
                path="soma/strategies/content_affiliate.py",
                diff="+    # back off on the failing path\n",
            )
        ],
    )
    decision = validate_patch(patch)
    ctx.audit.log_outcome(
        audit_id,
        "ok" if decision.ok else "rejected",
        {"decision": decision.model_dump()},
    )

    if not decision.ok:
        # Rejected patches are archived (negative lesson), never applied.
        ctx.audit.log_event(
            "mutation.archived",
            {"reason": decision.reason, "path": decision.rejected_path},
        )
        state.plan = {"action": "sleep", "reason": "patch rejected by validator"}
        state.route = "check_reproduction"
        return state

    state.plan = {
        "action": "evaluate_mutation",
        "failure_class": failure_class,
        "patch": patch.model_dump(),
    }
    state.route = "evaluate_mutation"
    return state
