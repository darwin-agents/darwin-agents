"""apply_mutation — promote a validated patch (docs/engineering/03).

In production the live Darwin is briefly paused at a safe checkpoint and the
patch is merged into the live soma. Layer 0 itself is *out of scope for
self-modification* (read for self-reflection, never patched), so the genome
never writes its own code; the **platform** performs the file write through
the optional ``ctx.soma_merge`` hook (a :class:`SomaMerger`, mirroring how
``ctx.patch_staging`` bridges to the staging service).

Two regimes:

- **Hook wired (Phase 1+).** The validated unified diff is applied to the
  Darwin's soma tree on disk. The fix counts as *landed* only when the
  merge succeeds; a failed merge keeps the recurring-failure counter (the
  fix never reached disk) and is archived as a negative lesson.
- **No hook (Phase 0).** We record the promotion and clear the
  recurring-failure counter — the behavioural effect a soma merge would
  have had on the loop's decision-making — without ever writing code.
"""

from __future__ import annotations

from types import SimpleNamespace


def run(state, ctx):
    failure_class = state.plan.get("failure_class", "")
    audit_id = ctx.audit.log_intent(
        "mutation.apply", {"failure_class": failure_class}
    )

    # Phase 1+: apply the real diff to the live soma when the platform has
    # wired the merge hook AND a patch travelled with the plan. ``None``
    # means no merge was attempted (Phase-0 simulation); a result with
    # ``merged=False`` means the merge was tried and failed.
    merge_result = None
    merge_hook = getattr(ctx, "soma_merge", None)
    patch_dict = state.plan.get("patch")
    if merge_hook is not None and patch_dict:
        merge_result = _merge_soma(patch_dict, failure_class, ctx)

    # The fix "landed" if we either ran the Phase-0 simulation (no merge)
    # or the real merge succeeded. Only then do we stop counting the class
    # as recurring — otherwise the loop must keep trying because the fix
    # never actually reached the soma.
    fix_landed = merge_result is None or bool(
        getattr(merge_result, "merged", False)
    )
    if fix_landed and failure_class in state.recurring_failures:
        del state.recurring_failures[failure_class]
    state.mutation_gate_open = False
    # A REAL patch reached disk → signal the platform to hot-reload the soma
    # at the next quiescent boundary so the rewrite changes behaviour this
    # life, not only at next boot (doc 33). Phase-0 simulation (no merge hook)
    # never sets it — there is no new code to load.
    if merge_result is not None and merge_result.merged:
        state.soma_reload_pending = True

    if merge_result is not None and not merge_result.merged:
        archive = getattr(ctx, "archive", None)
        if archive is not None:
            archive.record(
                failure_class, "merge_failed",
                getattr(merge_result, "reason", ""),
            )
        ctx.audit.log_event(
            "mutation.merge_failed",
            {"failure_class": failure_class,
             "reason": getattr(merge_result, "reason", ""),
             "rejected_path": getattr(merge_result, "rejected_path", None)},
        )

    # Path-C (doc 04 phase 3): if the proposal carries a neuron_proposal
    # AND the eval harness scored it favourably, submit + admit it into
    # the runtime catalog so the next dispatch can route to it. Failure
    # here is non-fatal — the behavior_delta still applies; the proposal
    # is just skipped.
    admitted_neuron: str | None = None
    proposal_dict = state.plan.get("neuron_proposal")
    if proposal_dict and getattr(ctx, "neuron_admission", None) is not None:
        admitted_neuron = _admit_proposal(proposal_dict, ctx)

    soma_merged = (
        bool(merge_result.merged) if merge_result is not None else None
    )
    merged_paths = (
        list(merge_result.applied_paths)
        if merge_result is not None and merge_result.merged
        else None
    )
    ctx.audit.log_outcome(
        audit_id, "ok",
        {"applied": fix_landed, "admitted_neuron": admitted_neuron,
         "soma_merged": soma_merged, "merged_paths": merged_paths},
    )
    ctx.audit.log_event(
        "mutation.applied",
        {"failure_class": failure_class,
         "admitted_neuron": admitted_neuron,
         "soma_merged": soma_merged,
         "merged_paths": merged_paths},
    )
    state.plan = {"action": "mutation_applied"}
    state.route = "check_reproduction"
    return state


def _merge_soma(patch_dict: dict, failure_class: str, ctx):
    """Build a :class:`Patch` from the plan dict and apply it via the
    platform merge hook. Returns the hook's result (duck-typed: ``.merged``
    / ``.reason`` / ``.applied_paths`` / ``.rejected_path``), or a
    failure-shaped namespace if the patch can't be built or the hook raises.
    Never raises — a self-modification failure must not crash the loop."""
    try:
        from ...machinery.patch_validator import FileChange, Patch
    except Exception:
        return None
    try:
        patch = Patch(
            rationale=str(patch_dict.get("rationale", "")),
            changes=[
                FileChange(path=c["path"], diff=c["diff"])
                for c in patch_dict.get("changes", [])
            ],
        )
    except Exception as exc:
        return SimpleNamespace(
            merged=False, reason=f"patch malformed: {exc}",
            applied_paths=(), rejected_path=None,
        )
    try:
        return ctx.soma_merge.merge(patch=patch, failure_class=failure_class)
    except Exception as exc:  # noqa: BLE001 — hook failures are non-fatal
        ctx.audit.log_event(
            "mutation.merge_error",
            {"failure_class": failure_class, "reason": str(exc)[:200]},
        )
        return SimpleNamespace(
            merged=False, reason=f"merge hook raised: {exc}",
            applied_paths=(), rejected_path=None,
        )


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
