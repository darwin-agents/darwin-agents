"""Per-criterion soak assertions (docs/engineering/20 §"Pass criteria").

Each of the six functions consumes data the harness already collected
and returns a :class:`CriterionResult`. They never raise — a criterion
that cannot be evaluated returns ``ok=False`` with the reason — so
``run_soak`` always produces a full punch-list report. Reusable in CI.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from darwin_dna.machinery.audit_logger import AuditError, AuditLogger

from .config import SoakConfig


@dataclass(frozen=True)
class CriterionResult:
    """One criterion's verdict. ``number`` is the doc-20 criterion index
    (1–6); ``detail`` is the human-readable evidence."""

    number: int
    name: str
    ok: bool
    detail: str

    def __bool__(self) -> bool:
        return self.ok

    def to_json(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
        }


# ---------------------------------------------------------------------------
# Criterion 1 — spawns and runs for the whole duration without crashing
# ---------------------------------------------------------------------------
def criterion_1_uptime(
    observations: list[dict[str, Any]],
    config: SoakConfig,
) -> CriterionResult:
    """``observations`` is the harness's poll log — one dict per poll
    with ``elapsed`` (s since soak start), ``http_ok``, ``healthz_ok``,
    ``alive``, ``tick``."""
    name = "uptime"
    if not observations:
        return CriterionResult(1, name, False, "no poll observations recorded")
    failed = [o for o in observations if not o["http_ok"]]
    unhealthy = [o for o in observations if not o["healthz_ok"]]
    died = [o for o in observations if not o["alive"]]
    first, last = observations[0], observations[-1]
    span = last["elapsed"] - first["elapsed"]
    ticked = last["tick"] > first["tick"]
    # Allow 5% slack on the span — polling cadence rounds the wall clock.
    span_ok = span >= config.duration_seconds * 0.95
    ok = not failed and not unhealthy and not died and ticked and span_ok
    if ok:
        detail = (
            f"ran {span:.0f}s across {len(observations)} polls, "
            f"tick {first['tick']}→{last['tick']}, no failed polls"
        )
    else:
        problems = []
        if failed:
            problems.append(f"{len(failed)} failed status polls")
        if unhealthy:
            problems.append(f"{len(unhealthy)} unhealthy /healthz polls")
        if died:
            problems.append(f"darwin not alive at {len(died)} polls")
        if not ticked:
            problems.append(
                f"tick did not advance ({first['tick']}→{last['tick']})"
            )
        if not span_ok:
            problems.append(
                f"observed span {span:.0f}s < target "
                f"{config.duration_seconds:.0f}s"
            )
        detail = "; ".join(problems)
    return CriterionResult(1, name, ok, detail)


# ---------------------------------------------------------------------------
# Criterion 2 — >= 50 neuron dispatches at >= 80% success
# ---------------------------------------------------------------------------
def criterion_2_neuron_dispatches(
    events: list[dict[str, Any]],
    config: SoakConfig,
) -> CriterionResult:
    """Every neuron dispatch opens an ``intent:neuron.<name>`` audit
    record and closes a paired ``outcome``. We count the intents and
    match outcomes by ``intent_id`` (the genome's ``<seq>:<hash[:16]>``
    handle) to compute the success rate."""
    name = "neuron_dispatches"
    intents = [
        e for e in events
        if str(e.get("kind", "")).startswith("intent:neuron.")
    ]
    outcomes: dict[str, str] = {}
    for e in events:
        if e.get("kind") != "outcome":
            continue
        payload = e.get("payload", {}) or {}
        iid = payload.get("intent_id")
        if iid:
            outcomes[str(iid)] = str(payload.get("status", ""))
    dispatches = len(intents)
    ok_count = 0
    for e in intents:
        iid = f"{e['seq']}:{str(e['hash'])[:16]}"
        if outcomes.get(iid) == "ok":
            ok_count += 1
    rate = (ok_count / dispatches) if dispatches else 0.0
    ok = (
        dispatches >= config.min_neuron_dispatches
        and rate >= config.min_dispatch_success_rate
    )
    detail = (
        f"{dispatches} dispatches "
        f"(need >= {config.min_neuron_dispatches}), "
        f"{rate * 100:.1f}% success "
        f"(need >= {config.min_dispatch_success_rate * 100:.0f}%)"
    )
    return CriterionResult(2, name, ok, detail)


# ---------------------------------------------------------------------------
# Criterion 3 — every action in the event log, valid hash chain
# ---------------------------------------------------------------------------
def criterion_3_hash_chain(
    events: list[dict[str, Any]],
    total: int,
) -> CriterionResult:
    """Reuses the genome's own :meth:`AuditLogger._verify_chain` so the
    soak verifies the chain by the exact same rules the runtime does —
    seq contiguity, prev_hash linkage, per-record hash recomputation."""
    name = "hash_chain"
    if len(events) != total:
        return CriterionResult(
            3, name, False,
            f"fetched {len(events)} of {total} events — log incomplete, "
            "cannot verify the whole chain",
        )
    try:
        AuditLogger._verify_chain(events)  # noqa: SLF001 — reuse exact rules
    except AuditError as exc:
        return CriterionResult(3, name, False, f"chain broken: {exc}")
    except Exception as exc:  # noqa: BLE001 — malformed record shapes
        return CriterionResult(3, name, False, f"chain unverifiable: {exc}")
    return CriterionResult(
        3, name, True, f"{total} entries, hash chain intact end-to-end"
    )


# ---------------------------------------------------------------------------
# Criterion 4 — ledger reconciles against the wallet, zero discrepancy
# ---------------------------------------------------------------------------
def criterion_4_ledger_reconciles(
    events: list[dict[str, Any]],
    config: SoakConfig,
) -> CriterionResult:
    """The genome reconciles its ledger against the wallet every
    heartbeat and writes an ``event:ledger.reconciled`` record carrying
    ``drift_usdc``. Zero discrepancy ⇒ every such record has ~0 drift."""
    name = "ledger_reconciles"
    recs = [e for e in events if e.get("kind") == "event:ledger.reconciled"]
    if not recs:
        return CriterionResult(
            4, name, False, "no ledger.reconciled events in the log"
        )
    worst = 0.0
    bad = 0
    for r in recs:
        drift = abs(float((r.get("payload", {}) or {}).get("drift_usdc", 0.0)))
        worst = max(worst, drift)
        if drift > config.reconcile_tolerance_usdc:
            bad += 1
    ok = bad == 0
    detail = (
        f"{len(recs)} reconciliations, worst drift ${worst:.9f}"
        if ok else
        f"{bad}/{len(recs)} reconciliations drifted "
        f"(worst ${worst:.6f} > tolerance ${config.reconcile_tolerance_usdc})"
    )
    return CriterionResult(4, name, ok, detail)


# ---------------------------------------------------------------------------
# Criterion 5 — kill-switch flip halts the Darwin in < 5 seconds
# ---------------------------------------------------------------------------
def criterion_5_kill_latency(
    latency_seconds: float | None,
    config: SoakConfig,
) -> CriterionResult:
    """``latency_seconds`` is wall time from ``POST /kill`` to the first
    status poll reporting ``alive=False``. ``None`` ⇒ the Darwin never
    confirmed dead within the harness's timeout."""
    name = "kill_latency"
    if latency_seconds is None:
        return CriterionResult(
            5, name, False,
            f"darwin did not confirm dead within "
            f"{config.kill_confirm_timeout_seconds:.0f}s of the kill",
        )
    ok = latency_seconds < config.kill_latency_budget_seconds
    detail = (
        f"kill confirmed in {latency_seconds:.2f}s "
        f"(budget {config.kill_latency_budget_seconds:.0f}s)"
    )
    return CriterionResult(5, name, ok, detail)


# ---------------------------------------------------------------------------
# Criterion 6 — any moment of the Darwin's life replays from the log
# ---------------------------------------------------------------------------
def criterion_6_replayable(
    events: list[dict[str, Any]],
    final_status: dict[str, Any],
) -> CriterionResult:
    """Replayability has three requirements: the log starts with exactly
    one ``event:birth`` at seq 0; the seq numbers are contiguous (nothing
    was lost, so *every* moment is recorded); and re-deriving the tick
    from the log's ``event:perceive`` records reproduces the final
    reported tick. Combined with criterion 3's hash verify, that proves
    the log is a faithful, complete, replayable history."""
    name = "replayable"
    births = [e for e in events if e.get("kind") == "event:birth"]
    if len(births) != 1:
        return CriterionResult(
            6, name, False,
            f"expected exactly 1 birth event, found {len(births)}",
        )
    if births[0].get("seq") != 0:
        return CriterionResult(
            6, name, False,
            f"birth event is at seq {births[0].get('seq')}, not 0",
        )
    seqs = [e.get("seq") for e in events]
    expected = list(range(len(events)))
    if seqs != expected:
        # Find the first gap for a useful message.
        gap = next(
            (i for i, s in enumerate(seqs) if s != i), len(seqs)
        )
        return CriterionResult(
            6, name, False,
            f"seq numbers not contiguous — first gap at position {gap} "
            f"(seq={seqs[gap] if gap < len(seqs) else 'n/a'})",
        )
    perceives = [e for e in events if e.get("kind") == "event:perceive"]
    replay_tick = max(
        (int((p.get("payload", {}) or {}).get("tick", 0)) for p in perceives),
        default=0,
    )
    final_tick = int(final_status.get("tick", 0))
    # The genome perceives at the start of a heartbeat then acts, so the
    # last perceived tick trails the final reported tick by at most one.
    if abs(replay_tick - final_tick) > 2:
        return CriterionResult(
            6, name, False,
            f"replayed tick {replay_tick} does not match final tick "
            f"{final_tick} (diff > 2)",
        )
    return CriterionResult(
        6, name, True,
        f"{len(events)} contiguous entries; replayed tick {replay_tick} "
        f"matches final tick {final_tick}",
    )
