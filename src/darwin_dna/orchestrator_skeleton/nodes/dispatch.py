"""dispatch — send work to a neuron (docs/engineering/04, the routing rules).

Selection (from 04): filter to neurons whose ``capability_tags`` ⊇ the task's
required tags; among those prefer lowest expected cost meeting the quality
bar; break ties by recency of success; if none match, file a capability
request and try the closest fallback. The *selection algorithm* is in the DNA;
the cost/quality weighting is soma-evolvable.

This node performs the only side effects in a heartbeat, so the kill switch is
polled here too, immediately before the neuron acts (P8). Escalations are
first-class: a blocked neuron files a ticket and the loop continues — it does
not fail the task (P4).
"""

from __future__ import annotations

from ..._untrusted import contains_fence_markers, wrap_untrusted
from ...machinery.audit_logger import AuditError


def _select(catalog, required_tags, recency):
    candidates = [
        n for n in catalog.values() if set(required_tags) <= set(n.capability_tags)
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda n: (n.cost_estimate.usd_p50, -recency.get(n.name, 0.0))
    )
    return candidates[0]


async def run(state, ctx):
    p = state.plan
    if p.get("action") != "dispatch" or not p.get("task"):
        state.route = "check_reproduction"
        return state

    # Side-effect boundary: poll before we touch the world.
    ctx.kill_switch.check()

    from ...neurons import Task

    task = Task.model_validate(p["task"])

    # Owner runtime control (doc 09): a task in a disabled business category
    # is not dispatched at all — the Darwin keeps running, this line of work
    # is simply parked while the owner has it switched off.
    if task.category in state.disabled_categories:
        ctx.audit.log_event(
            "dispatch.category_disabled", {"category": task.category}
        )
        state.route = "check_reproduction"
        return state

    # Owner runtime control (doc 09): a disabled neuron is removed from
    # selection (and ignored even if the plan named it explicitly).
    disabled = set(state.disabled_neurons)
    preselected = p.get("neuron") or ""
    if preselected in disabled:
        preselected = ""
    selectable = (
        {k: v for k, v in ctx.catalog.items() if k not in disabled}
        if disabled
        else ctx.catalog
    )
    # Recency-of-success tie-breaking is a soma refinement; the Layer 0
    # selector ships with an empty recency map (cost-only ordering).
    neuron = selectable.get(preselected) or _select(
        selectable, task.required_tags, {}
    )

    if neuron is None:
        # Distinguish an owner-disabled neuron from a genuine capability gap:
        # if the *unfiltered* catalog would have matched, the owner disabled
        # the only fit — that is a policy decision, not a missing capability.
        if disabled and _select(ctx.catalog, task.required_tags, {}) is not None:
            ctx.audit.log_event(
                "dispatch.neuron_disabled",
                {"tags": sorted(task.required_tags),
                 "disabled": sorted(disabled)},
            )
            state.route = "check_reproduction"
            return state
        # No neuron can do this — that's a capability gap, not a failure.
        for tag in task.required_tags:
            if tag not in state.pending_capability_requests:
                state.pending_capability_requests.append(tag)
        ctx.audit.log_event(
            "capability_request",
            {"reason": "no neuron matches", "tags": sorted(task.required_tags)},
        )
        state.route = "check_reproduction"
        return state

    nctx = ctx.build_neuron_context(neuron.name)
    state.dispatches_done += 1
    try:
        plan_obj = await neuron.plan(task, nctx)  # cheap, no side effects
        # P8: poll again immediately before the side-effecting call. plan()
        # may have taken time; a kill that arrived during it must stop the
        # action *before* execute(), not at the next node boundary.
        ctx.kill_switch.check()
        result = await neuron.execute(plan_obj, nctx)
    except AuditError:
        # P7 "no log, no action": if the audit logger failed we must HALT,
        # not treat it as a recoverable neuron crash. Propagate.
        raise
    except Exception as exc:  # a neuron blowing up must not kill the organism
        ctx.audit.log_event(
            "dispatch.neuron_crashed", {"neuron": neuron.name, "error": str(exc)}
        )
        _bump_failure(state, "neuron_crashed")
        # A neuron call is a write to episodic memory (docs/engineering/03) —
        # the crash is exactly the kind of thing the planner later asks "have
        # I seen this before?" about.
        ctx.episodic.record(
            tick=state.tick,
            signature=_sig(neuron.name, task),
            neuron=neuron.name,
            outcome="failed",
            error_code="neuron_crashed",
            ts=float(ctx.clock()),
        )
        state.route = "reflect"
        return state

    if result.escalated:
        # Escalate, don't fail (P4): the task does not error — it parks in
        # working memory and is resumed verbatim when the owner answers.
        # It is NOT counted approved/rejected yet; that happens on response.
        from ...constitution import classify_ticket

        blocker_kind = (result.error_code or "info")
        klass, timeout_s = classify_ticket(blocker_kind)
        ticket = result.output.get("ticket", f"esc-{neuron.name}-{state.tick}")
        opened_ts = float(ctx.clock())
        if ticket not in state.pending_escalations:
            state.pending_escalations.append(ticket)
        state.open_escalations[ticket] = {
            "ticket": ticket,
            "neuron": neuron.name,
            "task": task.model_dump(),
            "signature": _sig(neuron.name, task),
            "blocker_kind": blocker_kind,
            "ticket_class": klass.value,
            "opened_ts": opened_ts,
            "deadline_ts": opened_ts + timeout_s,
            "opened_tick": state.tick,
            "status": "open",
        }
        ctx.escalation.post(
            {
                "darwin_id": ctx.darwin_id,
                "ticket": ticket,
                "neuron": neuron.name,
                "ticket_class": klass.value,
                "tick": state.tick,
                "detail": result.output,
            }
        )
    elif result.ok:
        state.successful_dispatches += 1
        sid = ctx.strategy.spec.id
        if sid not in state.distinct_strategies:
            state.distinct_strategies.append(sid)
        if "content.write" in task.required_tags or task.kind == "content.write":
            state.content_pieces += 1
        if "web.publish" in task.required_tags or task.kind == "web.publish":
            state.published_pieces += 1
        # World-scraped text crosses into the orchestrator HERE — fence it at
        # the boundary before it can ever reach the planner (P-injection,
        # docs/engineering/10). The planner only ever sees the wrapped form.
        web_text = result.output.get("web_text")
        if web_text is not None:
            if contains_fence_markers(web_text):
                ctx.audit.log_event(
                    "untrusted.breakout_attempt",
                    {"neuron": neuron.name},  # the value itself is never logged
                )
            state.world_observations.append(wrap_untrusted(web_text))
            del state.world_observations[:-5]  # bounded; keep the 5 most recent
        # success clears that error class's recurrence counter
        if result.error_code and result.error_code in state.recurring_failures:
            del state.recurring_failures[result.error_code]
        # A resumed task that now succeeds closes its escalation for good.
        _close_escalations_for(state, _sig(neuron.name, task))
    else:
        # Policy refusals/blocked actions are a fitness counter-pressure
        # (docs/engineering/06, 10) — a Darwin that earns by ignoring the
        # constitution must lose on this term.
        if result.error_code == "policy_refused":
            state.policy_violations += 1
        _bump_failure(state, result.error_code or "unknown_error")

    # A neuron call is a write to episodic memory (docs/engineering/03): one
    # legible "I did X and Y happened" row the planner can recall later. The
    # signature is the same work-identity dispatch routes and pauses on.
    if result.escalated:
        outcome, code = "escalated", (result.error_code or "info")
    elif result.ok:
        outcome, code = "ok", None
    else:
        outcome, code = "failed", (result.error_code or "unknown_error")
    ctx.episodic.record(
        tick=state.tick,
        signature=_sig(neuron.name, task),
        neuron=neuron.name,
        outcome=outcome,
        error_code=code,
        ts=float(ctx.clock()),
    )

    state.route = "reflect"
    return state


def _bump_failure(state, code: str) -> None:
    state.recurring_failures[code] = state.recurring_failures.get(code, 0) + 1


def _sig(neuron_name: str, task) -> str:
    """Stable identity for "this kind of work" — used to (a) refuse to
    re-dispatch work that is parked on an open escalation and (b) match a
    resumed task back to the escalation that blocked it."""
    tags = ",".join(sorted(task.required_tags))
    return f"{neuron_name}|{task.kind}|{tags}"


def _close_escalations_for(state, signature: str) -> None:
    for tid, esc in list(state.open_escalations.items()):
        if esc.get("signature") == signature and esc.get("status") == "open":
            del state.open_escalations[tid]
            if tid in state.pending_escalations:
                state.pending_escalations.remove(tid)
