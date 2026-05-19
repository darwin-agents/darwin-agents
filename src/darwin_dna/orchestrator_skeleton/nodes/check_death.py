"""check_death — always run at the top of the loop (docs/engineering/05).

Treasury exhaustion is the hardest selection mechanism in the system. The
kill-switch poll is the runner's job (every node boundary, P8); this node owns
the *bankruptcy* condition:

    balance < minimum_viable_balance AND no revenue inbound within the grace
    window  =>  die.

The grace window exists because revenue is bursty (an affiliate commission
posts weekly). The hard floor — balance must stay above zero — is not
negotiable; minimum_viable_balance is set so the Darwin can afford its own
clean shutdown rather than crash mid-action.
"""

from __future__ import annotations


def run(state, ctx):
    hp = ctx.hp

    # Honour an already-decided death (e.g. ledger reconciliation drift set by
    # perceive). check_death is the single funnel into the graceful death
    # sequence, whatever the trigger.
    if not state.alive:
        if not state.death_reason:
            state.death_reason = "halted"
        ctx.audit.log_event("death_condition", {"reason": state.death_reason})
        state.route = "__end__"
        return state

    broke = state.balance_usdc < hp.minimum_viable_balance_usdc
    no_revenue = state.seconds_since_revenue > hp.revenue_grace_window_seconds

    if broke and no_revenue:
        state.alive = False
        state.death_reason = (
            f"bankruptcy: ${state.balance_usdc:.4f} < min-viable "
            f"${hp.minimum_viable_balance_usdc:.2f} and no revenue for "
            f"{state.seconds_since_revenue:.0f}s"
        )
        ctx.audit.log_event("death_condition", {"reason": state.death_reason})
        state.route = "__end__"
        return state

    state.route = "perceive"
    return state
