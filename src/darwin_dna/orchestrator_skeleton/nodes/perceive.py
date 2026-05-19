"""perceive — pull treasury, inbox, environment (docs/engineering/03).

Read-only refresh of the world into working memory. The build plan calls out
that the perceive node's safety checks are *restricted* from soma rewrites;
the only safety-critical thing it does here is keep the economic picture
honest so check_death and the treasury policy act on real numbers.
"""

from __future__ import annotations


def run(state, ctx):
    wallet = ctx.wallet
    state.balance_usdc = float(wallet.balance_usdc())

    # seconds_since_revenue drives the bankruptcy grace window. Prefer the
    # wallet's own answer; fall back to whatever the loop advanced.
    ssr = getattr(wallet, "seconds_since_revenue", None)
    if callable(ssr):
        state.seconds_since_revenue = float(ssr())

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
