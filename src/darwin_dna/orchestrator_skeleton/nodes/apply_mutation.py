"""apply_mutation — promote a validated patch (docs/engineering/03).

In production the live Darwin is briefly paused at a safe checkpoint and the
patch is merged into the live soma. Layer 0 itself is *out of scope for
self-modification* (read for self-reflection, never patched), so the genome
skeleton records the promotion and clears the recurring-failure counter that
triggered it — the behavioural effect a soma merge would have had on the
loop's decision-making — without ever writing genome code.
"""

from __future__ import annotations


def run(state, ctx):
    failure_class = state.plan.get("failure_class", "")
    audit_id = ctx.audit.log_intent(
        "mutation.apply", {"failure_class": failure_class}
    )
    # The patch addressed this class; stop counting it as recurring so the
    # loop returns to earning instead of re-proposing the same fix.
    if failure_class in state.recurring_failures:
        del state.recurring_failures[failure_class]
    state.mutation_gate_open = False
    ctx.audit.log_outcome(audit_id, "ok", {"applied": True})
    ctx.audit.log_event("mutation.applied", {"failure_class": failure_class})
    state.plan = {"action": "mutation_applied"}
    state.route = "check_reproduction"
    return state
