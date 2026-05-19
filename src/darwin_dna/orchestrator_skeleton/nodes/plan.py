"""plan — decide the next action (docs/engineering/03, 18).

The planner is the most important text in the project (its persistent system
prompt lives in ``prompts/planner_system.txt``). In Layer 0 it is deliberately
modest: it consults the strategy *prior*, the inherited *lessons*, and a
reasoning-tier LLM, then commits to exactly one of:

    dispatch a neuron · escalate to the owner · propose a mutation (gated) ·
    sleep

The LLM call is *advisory and resilient*: the genome must boot and keep
running even if the reasoning tier is degraded or its daily budget is spent
(docs/engineering/18 failure mode: "burns LLM money faster than it earns").
If the call fails we fall back to the strategy prior rather than stall — a
naive-but-alive Darwin is the Layer 0 bar.
"""

from __future__ import annotations

from ...machinery.llm_router import LLMError, Tier
from ...strategies import StrategyObservation


def run(state, ctx):
    # Earn-before-mutate: even when the gate is open, mutation is only chosen
    # when reflect has flagged a real recurring failure pattern. This biases
    # a young Darwin hard toward earning (docs/engineering/18).
    if state.mutation_gate_open:
        worst = max(state.recurring_failures.values(), default=0)
        if worst >= ctx.hp.min_recurring_failures_for_mutation:
            state.plan = {"action": "propose_mutation",
                          "failure_class": _worst_class(state)}
            state.last_action = "propose_mutation"
            ctx.audit.log_event("plan", {"action": "propose_mutation"})
            state.route = "propose_mutation"
            return state

    # Resume beats new work (P4): a parked task whose escalation the owner
    # just approved is re-dispatched verbatim — the work continues where it
    # paused, it was never failed.
    if state.resume_queue:
        item = state.resume_queue.pop(0)
        state.plan = {
            "action": "dispatch",
            "neuron": item["neuron"],
            "task": item["task"],
            "strategy_id": ctx.strategy.spec.id,
            "resumed_ticket": item.get("ticket"),
        }
        state.last_action = f"resume:{item['neuron']}"
        ctx.audit.log_event(
            "plan", {"action": "resume", "neuron": item["neuron"],
                     "ticket": item.get("ticket")}
        )
        state.route = "dispatch"
        return state

    obs = StrategyObservation(
        balance_usdc=state.balance_usdc,
        days_elapsed=state.days_elapsed,
        content_pieces=state.content_pieces,
        published_pieces=state.published_pieces,
        seconds_since_revenue=state.seconds_since_revenue,
        available_neurons=frozenset(state.available_neurons),
    )
    action = ctx.strategy.propose(obs)

    # Don't re-dispatch work that is parked on an open escalation — that would
    # just re-trigger the same blocker. Pause it (wait) until the owner
    # answers; the resume_queue path above brings it back. This is the
    # difference between "escalate, don't fail" and "fail in a loop".
    if action.kind == "dispatch" and action.task is not None:
        from .dispatch import _sig

        blocked = {
            e["signature"]
            for e in state.open_escalations.values()
            if e.get("status") == "open"
        }
        if _sig(action.neuron, action.task) in blocked:
            state.plan = {
                "action": "sleep",
                "reason": f"{action.neuron} task parked on open escalation",
            }
            state.last_action = "paused_on_escalation"
            ctx.audit.log_event(
                "plan", {"action": "paused_on_escalation",
                         "neuron": action.neuron}
            )
            state.route = "check_reproduction"
            return state

    # Advisory reasoning-tier consult — but NOT when the decision is to wait.
    # A founding Darwin that pays coding/reasoning tokens to "think" while it
    # has nothing to do is the textbook way to burn capital faster than it
    # earns (docs/engineering/18 failure mode). Idle is free; thinking is not.
    if action.kind != "wait":
        lessons = [l.statement for l in ctx.layer_stack.all_lessons()]
        try:
            ctx.llm.complete(
                Tier.REASONING,
                {
                    "task": "plan",
                    "strategy_prior": action.kind,
                    "balance_usdc": state.balance_usdc,
                    "lessons": lessons[:32],
                },
            )
        except LLMError as exc:
            ctx.audit.log_event("plan.llm_degraded", {"error": str(exc)})

    if action.kind == "dispatch":
        state.plan = {
            "action": "dispatch",
            "neuron": action.neuron,
            "task": action.task.model_dump() if action.task else None,
            "strategy_id": ctx.strategy.spec.id,
        }
        state.last_action = f"dispatch:{action.neuron}"
        state.route = "dispatch"
    elif action.kind == "capability_request":
        cap = action.requested_capability or "unknown"
        if cap not in state.pending_capability_requests:
            state.pending_capability_requests.append(cap)
        ctx.audit.log_event("capability_request", {"capability": cap,
                                                   "reason": action.reason})
        state.plan = {"action": "capability_request", "capability": cap}
        state.last_action = "capability_request"
        state.route = "check_reproduction"
    else:  # wait / sleep
        state.plan = {"action": "sleep", "reason": action.reason}
        state.last_action = "sleep"
        state.route = "check_reproduction"

    ctx.audit.log_event("plan", {"action": state.plan["action"],
                                 "reason": action.reason})
    return state


def _worst_class(state) -> str:
    if not state.recurring_failures:
        return ""
    return max(state.recurring_failures, key=state.recurring_failures.get)
