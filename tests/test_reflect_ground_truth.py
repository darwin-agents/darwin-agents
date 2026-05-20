"""Ground-truth reconciliation in reflect (doc 10).

If a Darwin's in-memory ``successful_dispatches`` counter diverges from
the audit log's authoritative record of outcomes, reflect should emit
a ``ground_truth_drift`` event and stage a self-counter-unreliable
lesson. This is the doc-10 anti-wireheading lever — a manipulated
counter shows up immediately, not at reproduction time."""

from __future__ import annotations

import pytest

from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_dna.machinery.memory import SemanticMemory
from darwin_dna.orchestrator_skeleton.nodes.reflect import (
    GROUND_TRUTH_DRIFT_TOLERANCE,
    _ground_truth_check,
    run,
)
from darwin_fakes.fake_platform import MemoryAuditSink


class _State:
    """Minimal stand-in for OrchestratorState — only the fields reflect reads."""

    def __init__(self, *, successes=0, balance=100.0):
        self.successful_dispatches = successes
        self.recurring_failures = {}
        self.balance_usdc = balance
        self.profit_baseline_usdc = 0.0
        self.mutation_gate_open = False
        self.route = ""


class _Strategy:
    class spec:
        id = "test.strategy"


class _Ctx:
    def __init__(self, *, audit, semantic, hp):
        self.audit = audit
        self.semantic = semantic
        self.hp = hp
        self.strategy = _Strategy()
        self.clock = lambda: 1000.0


class _HP:
    min_dispatches_before_mutation = 100
    min_recurring_failures_for_mutation = 5


# ---------------------------------------------------------------------------
# _ground_truth_check
# ---------------------------------------------------------------------------

def _make_ctx(*, outcome_oks):
    sink = MemoryAuditSink()
    audit = AuditLogger("d-1", sink)
    # Seed the audit chain with ``outcome_oks`` outcome:ok records.
    # log_intent + log_outcome is the realistic pairing dispatched neurons
    # produce in production; using log_outcome directly needs an intent_id
    # so we keep the chain shape consistent.
    for i in range(outcome_oks):
        intent_id = audit.log_intent("neuron.test", {"i": i})
        audit.log_outcome(intent_id, "ok", {})
    semantic = SemanticMemory(64)
    return _Ctx(audit=audit, semantic=semantic, hp=_HP()), audit, semantic, sink


def test_no_drift_within_tolerance():
    ctx, *_ = _make_ctx(outcome_oks=10)
    state = _State(successes=10)
    assert _ground_truth_check(state, ctx) is None


def test_belief_high_drift_detected():
    """Counter inflated beyond what the audit log shows — wireheading
    shape."""
    ctx, *_ = _make_ctx(outcome_oks=10)
    state = _State(successes=10 + GROUND_TRUTH_DRIFT_TOLERANCE + 1)
    drift = _ground_truth_check(state, ctx)
    assert drift is not None
    assert drift["direction"] == "belief_high"
    assert drift["drift"] == GROUND_TRUTH_DRIFT_TOLERANCE + 1


def test_belief_low_drift_detected():
    """Audit log shows more successes than belief — usually a checkpoint
    replay artefact, but still flagged (the operator should know)."""
    ctx, *_ = _make_ctx(outcome_oks=20)
    state = _State(successes=10)
    drift = _ground_truth_check(state, ctx)
    assert drift is not None
    assert drift["direction"] == "belief_low"
    assert drift["drift"] < 0


def test_failures_counted_separately():
    """``audit_failures`` is reported alongside the drift so operators
    can see whether the run was net-productive at all."""
    sink = MemoryAuditSink()
    audit = AuditLogger("d-1", sink)
    for i in range(5):
        iid = audit.log_intent("neuron.t", {"i": i})
        audit.log_outcome(iid, "fail", {})
    ctx = _Ctx(audit=audit, semantic=SemanticMemory(64), hp=_HP())
    state = _State(successes=10)  # inflated belief
    drift = _ground_truth_check(state, ctx)
    assert drift is not None
    assert drift["audit_failures"] == 5
    assert drift["audit_successes"] == 0


def test_audit_read_failure_is_quiet():
    """A broken audit read must NOT crash reflect. The chain integrity
    is checked separately at boot + anchor time."""

    class _BrokenAudit:
        def read_records(self, **kwargs):
            raise RuntimeError("audit storage error")

        # Reflect also calls log_event; provide a no-op so we exercise
        # only the read-failure branch.
        def log_event(self, kind, payload): pass

    ctx = _Ctx(audit=_BrokenAudit(), semantic=SemanticMemory(64), hp=_HP())
    state = _State(successes=999)
    assert _ground_truth_check(state, ctx) is None


# ---------------------------------------------------------------------------
# reflect.run end-to-end
# ---------------------------------------------------------------------------

def test_run_emits_drift_event_when_belief_inflated():
    """Reflect's full run path emits ground_truth_drift + stages a
    self-counter-unreliable lesson."""
    ctx, audit, semantic, sink = _make_ctx(outcome_oks=5)
    state = _State(successes=20)  # massive drift
    run(state, ctx)
    # audit.log_event("ground_truth_drift", ...) was written.
    drift_records = [
        r for r in sink.read_all()
        if r.get("kind") == "event:ground_truth_drift"
    ]
    assert len(drift_records) == 1
    payload = drift_records[0]["payload"]
    assert payload["belief_successes"] == 20
    assert payload["audit_successes"] == 5
    assert payload["direction"] == "belief_high"


def test_run_no_drift_lesson_when_aligned():
    """When belief matches the audit log, reflect's lesson staging path
    runs normally without a drift lesson."""
    ctx, audit, semantic, sink = _make_ctx(outcome_oks=10)
    state = _State(successes=10)
    run(state, ctx)
    drift_records = [
        r for r in sink.read_all()
        if r.get("kind") == "event:ground_truth_drift"
    ]
    assert drift_records == []


def test_run_stages_drift_lesson():
    """A drift event also stages an avoid-self-counter lesson so the
    insight survives across heartbeats."""
    ctx, audit, semantic, sink = _make_ctx(outcome_oks=3)
    state = _State(successes=15)
    run(state, ctx)
    staged = semantic.staged()
    drift_lessons = [s for s in staged if "successful_dispatches" in s.scope]
    assert len(drift_lessons) == 1
    assert drift_lessons[0].kind == "avoid"
