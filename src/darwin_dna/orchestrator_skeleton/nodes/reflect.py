"""reflect — post-action analysis (docs/engineering/03, 10, 18).

Updates the strategy's running picture and decides whether the
self-modification gate may open. The gate is deliberately strict
(docs/engineering/18): both conditions must hold —

    (a) ≥ MIN_DISPATCHES_BEFORE_MUTATION successful dispatches in life, and
    (b) a specific recurring failure pattern: ≥ N occurrences of the *same
        error class* (not the same message — variance must not look like a
        pattern).

This is the earn-before-mutate bias that stops a newborn burning its capital
on coding-tier refactors before it has earned anything.

**Ground-truth reconciliation (doc 10).** Reflect also compares the
state's in-memory beliefs (``successful_dispatches`` + recurring failure
counters) against the audit log's authoritative record. A drift past the
configured tolerance is itself a finding — staged as a structured
``drift`` lesson AND emitted as ``audit:ground_truth_drift`` so an
operator can see a Darwin whose self-model is diverging from the
audit-tracked reality."""

from __future__ import annotations


# Doc 10: tolerance for in-memory vs audit-log drift. Below this the
# difference is just paths that haven't fully closed yet (in-flight
# outcomes); above it the Darwin's model of itself disagrees with its
# durable record. Tight enough to catch a wireheaded counter, loose
# enough to forgive normal in-flight skew.
GROUND_TRUTH_DRIFT_TOLERANCE = 3


def _ground_truth_check(state, ctx) -> dict | None:
    """Compare ``state.successful_dispatches`` to the audit log's
    lifetime count of successful neuron-dispatch outcomes. A drift
    bigger than the tolerance indicates either (a) a wireheaded counter
    (the Darwin manipulated its own bookkeeping) or (b) audit records
    that are out of sync (race conditions, partial writes). Either way,
    surface it.

    The count comes from :meth:`AuditLogger.dispatch_outcome_counts` —
    an O(1) running tally the logger maintains per commit. reflect runs
    every heartbeat, so re-scanning the chain here would be O(n²) over a
    life; and the tally is lifetime-vs-lifetime, so the comparison is
    dimensionally honest no matter how long the Darwin has run.

    Returns a drift report dict (when over tolerance) or None."""
    try:
        counts = ctx.audit.dispatch_outcome_counts()
    except Exception:
        # A read failure shouldn't poison reflect; the chain integrity is
        # checked separately at boot + anchor time.
        return None
    audit_ok = int(counts.get("ok", 0))
    audit_fail = int(counts.get("fail", 0))
    drift_ok = state.successful_dispatches - audit_ok
    abs_drift = abs(drift_ok)
    if abs_drift <= GROUND_TRUTH_DRIFT_TOLERANCE:
        return None
    return {
        "belief_successes": state.successful_dispatches,
        "audit_successes": audit_ok,
        "audit_failures": audit_fail,
        "drift": drift_ok,
        "tolerance": GROUND_TRUTH_DRIFT_TOLERANCE,
        # A positive drift = belief > audit (counter inflated relative to
        # ground truth; the wireheading shape). Negative = audit > belief
        # (lost counter — usually a checkpoint-replay artefact, not the
        # security-relevant case).
        "direction": "belief_high" if drift_ok > 0 else "belief_low",
    }


def run(state, ctx):
    hp = ctx.hp

    enough_experience = state.successful_dispatches >= hp.min_dispatches_before_mutation
    worst_recurrence = max(state.recurring_failures.values(), default=0)
    has_pattern = worst_recurrence >= hp.min_recurring_failures_for_mutation

    state.mutation_gate_open = bool(enough_experience and has_pattern)

    # profit since the last reproduction, read from the (reconciled) balance
    state_profit = state.balance_usdc - state.profit_baseline_usdc

    # Reflect decides what is a lesson (docs/engineering/03). Stage the
    # structured candidate in semantic memory; evidence accrues per heartbeat
    # via the dedupe-by-scope increment, and the strongest survive to the next
    # reproduction even if the transient counter that produced them clears
    # first. That persistence is the entire point of a staging ground.
    if has_pattern:
        worst = max(state.recurring_failures, key=state.recurring_failures.get)
        ctx.semantic.stage(
            kind="avoid",
            scope=f"failure:{worst}",
            statement=(
                f"the {worst!r} failure recurred {worst_recurrence}x within a "
                f"single generation; prefer a path that does not hit it"
            ),
            payload={"failure_class": worst, "observed_count": worst_recurrence},
            ts=float(ctx.clock()),
        )
    if enough_experience and state_profit > 0:
        sid = ctx.strategy.spec.id
        ctx.semantic.stage(
            kind="prefer",
            scope=f"strategy:{sid}",
            statement=(
                f"{sid!r} sustained {state.successful_dispatches} successful "
                f"dispatches while net-profitable; a reasonable first try"
            ),
            payload={"strategy_id": sid,
                     "profit_usdc": round(state_profit, 2)},
            ts=float(ctx.clock()),
        )

    ctx.audit.log_event(
        "reflect",
        {
            "successful_dispatches": state.successful_dispatches,
            "worst_recurrence": worst_recurrence,
            "mutation_gate_open": state.mutation_gate_open,
            "profit_since_reproduction": round(state_profit, 6),
        },
    )

    # Ground-truth reconciliation (doc 10). Compare in-memory beliefs to
    # the audit log; drift past tolerance is a self-model issue worth
    # staging AND emitting so an operator dashboard sees it.
    drift = _ground_truth_check(state, ctx)
    if drift is not None:
        ctx.audit.log_event("ground_truth_drift", drift)
        # Stage a structured lesson so the *next* reflect doesn't have to
        # re-discover this signal; downstream reproduction may want to
        # carry a "self-counter unreliable" warning into the next layer.
        ctx.semantic.stage(
            kind="avoid",
            scope="self.counter:successful_dispatches",
            statement=(
                f"in-memory successful_dispatches ({drift['belief_successes']}) "
                f"diverged from the audit log ({drift['audit_successes']}) by "
                f"{drift['drift']:+d}; do not trust the counter alone"
            ),
            payload=drift,
            ts=float(ctx.clock()),
        )

    state.route = "check_reproduction"
    return state
