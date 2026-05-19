"""perceive — pull treasury, inbox, environment (docs/engineering/03, 08).

Read-only refresh of the world into working memory, plus the escalation
inbox: it applies owner resolutions to parked work (resume on approve, drop
on deny) and sweeps timed-out tickets to a deterministic deny so a Darwin is
never silently stuck (P4). The build plan calls out that perceive's safety
checks are restricted from soma rewrites; keeping the economic picture honest
and resolving escalations deterministically are both that kind of check.
"""

from __future__ import annotations


def _resolve_escalations(state, ctx) -> None:
    # 1. Owner resolutions since last poll.
    for resp in ctx.escalation.poll_responses(ctx.darwin_id):
        tid = resp.get("ticket")
        esc = state.open_escalations.get(tid)
        if esc is None or esc.get("status") != "open":
            continue
        decision = resp.get("decision")
        if decision in ("approve", "data"):
            task = dict(esc["task"])
            if decision == "data" and isinstance(resp.get("data"), dict):
                payload = dict(task.get("payload", {}))
                payload.update(resp["data"])
                task["payload"] = payload
            state.resume_queue.append(
                {"neuron": esc["neuron"], "task": task, "ticket": tid}
            )
            state.escalations_approved += 1
            ctx.audit.log_event(
                "escalation.resolved",
                {"ticket": tid, "decision": "approve", "neuron": esc["neuron"]},
            )
        else:  # deny
            state.escalations_rejected += 1
            ctx.audit.log_event(
                "escalation.resolved",
                {"ticket": tid, "decision": "deny", "neuron": esc["neuron"]},
            )
        del state.open_escalations[tid]
        if tid in state.pending_escalations:
            state.pending_escalations.remove(tid)

    # 2. Timeout sweep — a missed deadline is a deterministic deny + re-plan,
    #    never a hang (docs/engineering/08). Counts as a rejected escalation
    #    (the owner effectively ignored it — a quality signal).
    now = float(ctx.clock())
    for tid, esc in list(state.open_escalations.items()):
        if esc.get("status") == "open" and now > esc["deadline_ts"]:
            state.escalations_rejected += 1
            ctx.audit.log_event(
                "escalation.timeout",
                {"ticket": tid, "ticket_class": esc["ticket_class"],
                 "neuron": esc["neuron"]},
            )
            del state.open_escalations[tid]
            if tid in state.pending_escalations:
                state.pending_escalations.remove(tid)


def run(state, ctx):
    wallet = ctx.wallet
    state.balance_usdc = float(wallet.balance_usdc())

    # seconds_since_revenue drives the bankruptcy grace window. Prefer the
    # wallet's own answer; fall back to whatever the loop advanced.
    ssr = getattr(wallet, "seconds_since_revenue", None)
    if callable(ssr):
        state.seconds_since_revenue = float(ssr())

    _resolve_escalations(state, ctx)

    state.available_neurons = sorted(ctx.catalog)
    if state.starting_capital_usdc <= 0:
        state.starting_capital_usdc = state.balance_usdc
        state.profit_baseline_usdc = state.balance_usdc

    ctx.audit.log_event(
        "perceive",
        {
            "tick": state.tick,
            "balance_usdc": state.balance_usdc,
            "content_pieces": state.content_pieces,
        },
    )
    state.route = "plan"
    return state
