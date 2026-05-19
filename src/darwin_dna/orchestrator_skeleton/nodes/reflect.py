"""reflect — post-action analysis (docs/engineering/03, 18).

Updates the strategy's running picture and decides whether the
self-modification gate may open. The gate is deliberately strict
(docs/engineering/18): both conditions must hold —

    (a) ≥ MIN_DISPATCHES_BEFORE_MUTATION successful dispatches in life, and
    (b) a specific recurring failure pattern: ≥ N occurrences of the *same
        error class* (not the same message — variance must not look like a
        pattern).

This is the earn-before-mutate bias that stops a newborn burning its capital
on coding-tier refactors before it has earned anything.
"""

from __future__ import annotations


def run(state, ctx):
    hp = ctx.hp

    enough_experience = state.successful_dispatches >= hp.min_dispatches_before_mutation
    worst_recurrence = max(state.recurring_failures.values(), default=0)
    has_pattern = worst_recurrence >= hp.min_recurring_failures_for_mutation

    state.mutation_gate_open = bool(enough_experience and has_pattern)

    # profit since the last reproduction, read from the (reconciled) balance
    state_profit = state.balance_usdc - state.profit_baseline_usdc

    ctx.audit.log_event(
        "reflect",
        {
            "successful_dispatches": state.successful_dispatches,
            "worst_recurrence": worst_recurrence,
            "mutation_gate_open": state.mutation_gate_open,
            "profit_since_reproduction": round(state_profit, 6),
        },
    )
    state.route = "check_reproduction"
    return state
