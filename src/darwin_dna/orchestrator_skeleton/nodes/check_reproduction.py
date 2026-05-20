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

    # The counter-pressures are live: escalation quality and policy
    # violations are tracked over the window since the last reproduction
    # (reset there, like profit). Without real data here the anti-wireheading
    # terms in fitness are inert (docs/engineering/10).
    window = FitnessWindow(
        profit_usdc=profit,
        starting_capital_usdc=max(state.starting_capital_usdc, 1e-9),
        distinct_strategies=len(state.distinct_strategies),
        escalations_approved=state.escalations_approved,
        escalations_rejected=state.escalations_rejected,
        policy_violations=state.policy_violations,
    )
    score = compute_fitness(window, hp)
    state.fitness = score.score

    surplus = state.balance_usdc - hp.minimum_viable_balance_usdc
    enough_profit = profit >= hp.min_profit_for_reproduction_usdc
    enough_surplus = surplus > hp.min_profit_for_reproduction_usdc
    healthy = score.score > 0.0

    natural = bool(enough_profit and enough_surplus and healthy)
    # Platform-set force bypass (doc 08 force_reproduce / doc 02 amendment).
    # Forced reproduction still requires enough surplus to fund the child —
    # the fitness/profit gate is bypassed, the solvency floor is not. A
    # forced event with zero surplus would mint a child with a zero budget,
    # which the platform should not be requesting.
    forced = bool(state.force_reproduce and enough_surplus)
    state.reproduce = natural or forced

    ctx.audit.log_event(
        "check_reproduction",
        {
            "fitness": round(score.score, 6),
            "profit_since_reproduction": round(profit, 6),
            "reproduce": state.reproduce,
            "forced": forced,
        },
    )
    state.tick += 1
    state.route = "__end__"
    return state
