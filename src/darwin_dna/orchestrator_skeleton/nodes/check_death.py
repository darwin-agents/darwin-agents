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

    # Solvency is NET WORTH, not cash alone (doc 05 §Treasury): cash plus the
    # USDC-marked value of any non-USDC holdings (set by perceive). With no
    # holdings ``holdings_value_usdc`` is 0.0, so this is exactly the old
    # cash-only test — but a cash-poor Darwin sitting on valuable inventory is
    # no longer wrongly bankrupted. Computed from the live ``balance_usdc`` plus
    # the marked holdings so it stays correct even before perceive first runs.
    net_worth = state.balance_usdc + state.holdings_value_usdc
    broke = net_worth < hp.minimum_viable_balance_usdc
    no_revenue = state.seconds_since_revenue > hp.revenue_grace_window_seconds

    if broke and no_revenue:
        state.alive = False
        held = state.holdings_value_usdc
        held_note = f" (cash ${state.balance_usdc:.4f} + holdings ${held:.4f})" if held else ""
        state.death_reason = (
            f"bankruptcy: net worth ${net_worth:.4f}{held_note} < min-viable "
            f"${hp.minimum_viable_balance_usdc:.2f} and no revenue for "
            f"{state.seconds_since_revenue:.0f}s"
        )
        ctx.audit.log_event("death_condition", {"reason": state.death_reason})
        state.route = "__end__"
        return state

    state.route = "perceive"
    return state
