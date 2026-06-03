"""perceive — pull treasury, inbox, environment (docs/engineering/03, 08).

Read-only refresh of the world into working memory, plus the escalation
inbox: it applies owner resolutions to parked work (resume on approve, drop
on deny) and sweeps timed-out tickets to a deterministic deny so a Darwin is
never silently stuck (P4). The build plan calls out that perceive's safety
checks are restricted from soma rewrites; keeping the economic picture honest
and resolving escalations deterministically are both that kind of check.
"""

from __future__ import annotations

from ...machinery.ledger import LedgerError


def _reconcile_ledger(state, ctx) -> bool:
    """Journal new authoritative wallet movements into the double-entry
    ledger and reconcile it against the wallet (doc 05). A drift beyond
    tolerance is not recoverable — the genome halts itself gracefully (the
    sim's I7 invariant, now enforced from inside the organism). Returns True
    if it triggered a halt."""
    wallet = ctx.wallet
    ledger = ctx.ledger
    movements_fn = getattr(wallet, "movements", None)
    if ledger is None or not callable(movements_fn):
        return False  # no authoritative feed → nothing to reconcile against
    movs = movements_fn()
    now = float(ctx.clock())
    bal = float(wallet.balance_usdc())

    if not ledger.seeded:
        # Derive the opening so a mid-life boot reconciles by construction;
        # divergence is then anything that moves the wallet without a
        # corresponding movement in the feed.
        net = 0.0
        for mv in movs:
            if "credit" in mv:
                net += float(mv["credit"]["amount_usdc"])
            elif "debit" in mv:
                net -= float(mv["debit"]["amount_usdc"])
            elif "reproduction_transfer" in mv:
                net -= float(mv["reproduction_transfer"]["amount_usdc"])
        ledger.seed_opening(bal - net, ts=now)

    ledger.journal_new(movs, now=now)
    try:
        drift = ledger.reconcile(
            bal, ctx.hp.ledger_reconciliation_tolerance_usdc
        )
    except LedgerError as exc:
        ctx.audit.log_event("ledger.reconciliation_failed", {"error": str(exc)})
        state.alive = False
        state.death_reason = f"ledger reconciliation drift: {exc}"
        state.route = "check_death"  # funnel into the graceful death sequence
        return True

    ctx.audit.log_event(
        "ledger.reconciled",
        {"drift_usdc": round(drift, 9),
         "treasury_usdc": ledger.treasury_balance()},
    )
    return False


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
            # Thread any owner-supplied data into the resumed task payload.
            # Both "data" (owner answered a question) and "approve" (owner
            # approved + the platform may have attached a one-time vault
            # grant token, doc 16) carry it through so the retrying neuron
            # sees e.g. ``payload["grant_token"]`` for a sensitive read.
            if isinstance(resp.get("data"), dict) and resp["data"]:
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

    # Mark non-USDC holdings to USDC (doc 05 §Treasury). ``balance_usdc`` is
    # cash only; a trading Darwin's inventory is folded into net worth here so
    # the bankruptcy check below cannot kill a cash-poor-but-holding-rich
    # Darwin. No-op (0.0) when nothing is held → net worth == cash, unchanged.
    state.holdings_value_usdc = 0.0
    holdings = getattr(ctx, "holdings", None)
    if holdings is not None and not holdings.is_empty:
        mtm = holdings.mark_to_usdc(getattr(ctx, "price_oracle", None))
        state.holdings_value_usdc = float(mtm.value_usdc)
        ctx.audit.log_event(
            "holdings.marked_to_market",
            {
                "value_usdc": state.holdings_value_usdc,
                "marked": mtm.marked,
                # Symbols valued at cost basis because the oracle had no quote —
                # a data gap a real platform should monitor (stale/missing feed).
                "cost_basis_fallback": mtm.fallback_symbols or None,
            },
        )
    state.net_worth_usdc = state.balance_usdc + state.holdings_value_usdc

    # Reconcile the double-entry ledger against the authoritative wallet
    # every heartbeat (doc 05). A drift beyond tolerance halts the Darwin —
    # bail out of perceive immediately, the route is already check_death.
    if _reconcile_ledger(state, ctx):
        return state

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
