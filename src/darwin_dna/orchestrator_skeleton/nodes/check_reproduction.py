"""check_reproduction — should we spawn a child? (docs/engineering/06)

Reproduction fires when fitness over the rolling window clears the bar AND
the treasury has surplus to fund a child. This node only *decides and records*
— the actual reproduction event (distill → validate → sign → spawn → register)
is run by ``evolution/reproduction.py`` from the loop, because it needs the
platform signing service and a fresh wallet, which nodes do not get to touch.

The fitness weights are read from the DNA and are not evolvable: reweighting
your own fitness is the textbook wireheading path (06, 10).
"""

from __future__ import annotations

from ...machinery.fitness import FitnessWindow, compute_fitness


def run(state, ctx):
    hp = ctx.hp
    profit = state.balance_usdc - state.profit_baseline_usdc

    window = FitnessWindow(
        profit_usdc=profit,
        starting_capital_usdc=max(state.starting_capital_usdc, 1e-9),
        distinct_strategies=len(state.distinct_strategies),
        escalations_approved=0,  # owner-response tracking is a platform concern
        escalations_rejected=0,
        policy_violations=0,
    )
    score = compute_fitness(window, hp)
    state.fitness = score.score

    surplus = state.balance_usdc - hp.minimum_viable_balance_usdc
    enough_profit = profit >= hp.min_profit_for_reproduction_usdc
    enough_surplus = surplus > hp.min_profit_for_reproduction_usdc
    healthy = score.score > 0.0

    state.reproduce = bool(enough_profit and enough_surplus and healthy)

    ctx.audit.log_event(
        "check_reproduction",
        {
            "fitness": round(score.score, 6),
            "profit_since_reproduction": round(profit, 6),
            "reproduce": state.reproduce,
        },
    )
    state.tick += 1
    state.route = "__end__"
    return state
