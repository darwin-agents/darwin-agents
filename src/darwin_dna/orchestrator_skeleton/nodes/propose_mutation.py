"""propose_mutation — the gated self-modification entry (docs/engineering/03, 21).

A coding-tier model is asked for a patch (a unified diff) targeting the
recurring failure. The patch is run through the in-DNA patch validator
*before* anything else — a patch that touches the genome, the validator, the
kill-switch poll, the audit calls, or the treasury enforcement points is
rejected here and archived as a negative lesson. The Darwin cannot rewrite the
thing that judges its rewrites.

The output carries BOTH a unified diff (audit-legible; replayable in the
patch-staging sandbox) AND a structured ``BehaviorDelta`` (executable; what
the Layer-0 harness compares as the candidate trajectory). The unified diff
is what Phase 1's :class:`PatchStagingService` applies to the soma copy
before running the sandboxed harness; the BehaviorDelta is what both paths
score. A proposer that does not supply a diff still flows: a single-line
default is fabricated so the patch-validator surface is exercised and the
audit chain records a concrete artifact even when the model was terse.
"""

from __future__ import annotations

from ...machinery.llm_router import LLMError, Tier, acomplete
from ...machinery.patch_validator import FileChange, Patch, validate_patch


async def run(state, ctx):
    failure_class = state.plan.get("failure_class", "")

    # Don't spend coding-tier budget re-deriving a known dead end (doc 06:
    # the archive carries negative lessons so the lineage doesn't re-try the
    # same path).
    archive = getattr(ctx, "archive", None)
    if archive is not None and failure_class in archive.dead_ends():
        ctx.audit.log_event(
            "mutation.skipped_dead_end", {"failure_class": failure_class}
        )
        state.plan = {"action": "sleep", "reason": "known dead end; not re-proposing"}
        state.route = "check_reproduction"
        return state

    audit_id = ctx.audit.log_intent(
        "mutation.propose", {"failure_class": failure_class}
    )

    try:
        resp = await acomplete(
            ctx.llm,
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

    if archive is not None:
        archive.record(failure_class, "proposed", patch.rationale)

    if not decision.ok:
        # Rejected patches are archived (negative lesson), never applied.
        if archive is not None:
            archive.record(failure_class, "rejected",
                            decision.reason or "validator rejected")
        ctx.audit.log_event(
            "mutation.archived",
            {"reason": decision.reason, "path": decision.rejected_path},
        )
        state.plan = {"action": "sleep", "reason": "patch rejected by validator"}
        state.route = "check_reproduction"
        return state

    # The candidate's *executable* form (the diff travels for audit only).
    # The proposer must supply it — you can only empirically test something
    # you can run (P5).
    delta = resp.get("behavior_delta") or {
        "kind": "backoff", "failure_class": failure_class, "after_failures": 3
    }
    # Path-C: the LLM may additionally propose a new neuron via
    # ``resp["neuron_proposal"]``. Doc 04 phase 3. The proposal travels
    # alongside the BehaviorDelta and is evaluated by the harness; an
    # admitted proposal joins the catalog at apply_mutation time. The
    # admission gate's contract validator is also called here so we
    # surface invalid proposals with a clear reason rather than letting
    # them blow up at apply time.
    proposal_payload = resp.get("neuron_proposal")
    proposal_dict = None
    if proposal_payload is not None:
        proposal_dict = _validate_and_normalise_proposal(
            proposal_payload, ctx,
        )
        if proposal_dict is None:
            ctx.audit.log_event(
                "neuron_proposal.skipped",
                {"failure_class": failure_class,
                 "reason": "invalid proposal payload"},
            )
    state.plan = {
        "action": "evaluate_mutation",
        "failure_class": failure_class,
        "patch": patch.model_dump(),
        "behavior_delta": delta,
        "neuron_proposal": proposal_dict,
        "recurrence": int(state.recurring_failures.get(failure_class, 1)),
    }
    state.route = "evaluate_mutation"
    return state


def _validate_and_normalise_proposal(payload, ctx) -> dict | None:
    """Coerce the LLM's free-form proposal into a NeuronProposal that
    passes the contract validator. Returns the dict form on success, or
    None when the payload is unusable. Never raises so a malformed
    proposal doesn't poison the mutation flow."""
    if not isinstance(payload, dict):
        return None
    try:
        from ...machinery.neuron_proposal import (
            NeuronProposal,
            NeuronProposalError,
            validate_proposal,
        )
    except Exception:
        return None
    try:
        proposal = NeuronProposal(
            name=str(payload.get("name", "")),
            rationale=str(payload.get("rationale", "")),
            capability_tags=frozenset(payload.get("capability_tags", [])),
            steps=tuple(payload.get("steps", [])),
            est_cost_usdc_p50=float(payload.get("est_cost_usdc_p50", 0.0)),
            est_cost_usdc_p95=float(payload.get("est_cost_usdc_p95", 0.0)),
        )
    except Exception as exc:
        ctx.audit.log_event(
            "neuron_proposal.malformed",
            {"reason": str(exc)[:200]},
        )
        return None
    try:
        validate_proposal(proposal, existing_catalog=ctx.catalog)
    except NeuronProposalError as exc:
        ctx.audit.log_event(
            "neuron_proposal.refused",
            {"name": proposal.name, "reason": str(exc)},
        )
        return None
    return proposal.to_json()
