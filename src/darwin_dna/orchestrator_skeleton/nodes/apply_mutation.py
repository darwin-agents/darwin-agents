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

    # Path-C (doc 04 phase 3): if the proposal carries a neuron_proposal
    # AND the eval harness scored it favourably, submit + admit it into
    # the runtime catalog so the next dispatch can route to it. Failure
    # here is non-fatal — the behavior_delta still applies; the proposal
    # is just skipped.
    admitted_neuron: str | None = None
    proposal_dict = state.plan.get("neuron_proposal")
    if proposal_dict and getattr(ctx, "neuron_admission", None) is not None:
        admitted_neuron = _admit_proposal(proposal_dict, ctx)

    ctx.audit.log_outcome(
        audit_id, "ok",
        {"applied": True, "admitted_neuron": admitted_neuron},
    )
    ctx.audit.log_event(
        "mutation.applied",
        {"failure_class": failure_class,
         "admitted_neuron": admitted_neuron},
    )
    state.plan = {"action": "mutation_applied"}
    state.route = "check_reproduction"
    return state


def _admit_proposal(proposal_dict: dict, ctx) -> str | None:
    """Submit + admit a Path-C neuron proposal. Returns the admitted
    name on success, None on any refusal (validator, contract check,
    cap reached). Never raises so a bad proposal doesn't poison the
    apply step."""
    try:
        from ...machinery.neuron_proposal import (
            NeuronProposal,
            NeuronProposalError,
        )
    except Exception:
        return None
    try:
        proposal = NeuronProposal(
            name=str(proposal_dict.get("name", "")),
            rationale=str(proposal_dict.get("rationale", "")),
            capability_tags=frozenset(
                proposal_dict.get("capability_tags", [])
            ),
            steps=tuple(proposal_dict.get("steps", [])),
            est_cost_usdc_p50=float(
                proposal_dict.get("est_cost_usdc_p50", 0.0)
            ),
            est_cost_usdc_p95=float(
                proposal_dict.get("est_cost_usdc_p95", 0.0)
            ),
        )
    except Exception:
        return None
    gate = ctx.neuron_admission
    try:
        gate.submit(proposal, existing_catalog=ctx.catalog)
    except NeuronProposalError as exc:
        ctx.audit.log_event(
            "neuron_proposal.submit_refused",
            {"name": proposal.name, "reason": str(exc)},
        )
        return None
    # Mandatory human review (doc 04 phase 3, default ON): a submitted
    # proposal sits in the gate's staging quarantine and is NOT merged into
    # the live catalog until an operator approves it (via the platform's
    # admit endpoint). File a review escalation and stop here — do NOT
    # auto-admit against live traffic.
    if getattr(ctx, "path_c_require_human_review", True):
        poster = getattr(getattr(ctx, "escalation", None), "post", None)
        if callable(poster):
            try:
                poster({
                    "darwin_id": getattr(ctx, "darwin_id", None),
                    "ticket": f"pathc-review-{proposal.name}",
                    "ticket_class": "approval",
                    "kind": "neuron_admission_review",
                    "detail": {
                        "name": proposal.name,
                        "rationale": proposal.rationale,
                        "capability_tags": sorted(proposal.capability_tags),
                    },
                })
            except Exception:
                pass
        ctx.audit.log_event(
            "neuron_proposal.pending_review",
            {"name": proposal.name},
        )
        return None  # staged, not admitted — awaits operator approval
    # Auto-admit (operator opted out of human review):
    # use the runtime helper if available — that's the path that
    # also re-runs the catalog loader's contract check + updates
    # ctx.catalog in place.
    admit_via_runtime = getattr(ctx, "admit_neuron_proposal_helper", None)
    if callable(admit_via_runtime):
        try:
            admit_via_runtime(proposal.name)
            return proposal.name
        except Exception:
            return None
    # Fallback: gate-only admit (no catalog merge — useful for tests
    # that don't have a full runtime in the context).
    try:
        gate.admit(proposal.name)
        return proposal.name
    except NeuronProposalError as exc:
        ctx.audit.log_event(
            "neuron_proposal.admit_refused",
            {"name": proposal.name, "reason": str(exc)},
        )
        return None
