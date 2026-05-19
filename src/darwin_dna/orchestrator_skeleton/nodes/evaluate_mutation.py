"""evaluate_mutation — empirical validation, not proof (P5, docs/engineering/03).

In production this stages the patch in a fresh E2B sandbox against a frozen
evaluation harness (replayed real situations + simulated revenue/cost) and
asks: did the patched soma make better decisions and stay within the
invariants? The harness is part of the DNA so a Darwin cannot grade its own
rewrites by softer rules.

The Layer 0 skeleton ships a conservative deterministic stand-in for that
harness: a patch "passes" only if it is validator-clean (already checked) and
its rationale actually references the recurring failure class it claims to
fix. Anything else is archived. Real empirical scoring arrives with the
simulation harness.
"""

from __future__ import annotations


def run(state, ctx):
    p = state.plan
    patch = p.get("patch") or {}
    failure_class = p.get("failure_class", "")

    rationale = (patch.get("rationale") or "").lower()
    targeted = bool(failure_class) and failure_class.lower() in rationale

    audit_id = ctx.audit.log_intent(
        "mutation.evaluate", {"failure_class": failure_class}
    )
    if targeted:
        ctx.audit.log_outcome(audit_id, "ok", {"verdict": "promote"})
        state.plan = {"action": "apply_mutation", "failure_class": failure_class}
        state.route = "apply_mutation"
    else:
        ctx.audit.log_outcome(audit_id, "ok", {"verdict": "archive"})
        ctx.audit.log_event(
            "mutation.archived",
            {"reason": "did not empirically target the failure", "failure_class": failure_class},
        )
        state.plan = {"action": "sleep", "reason": "mutation archived"}
        state.route = "check_reproduction"
    return state
