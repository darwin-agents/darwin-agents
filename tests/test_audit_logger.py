"""Paranoid audit-logger tests (docs/engineering/18 hot path #3, P7).

Write-before-act, unbroken hash chain across restarts, serialized concurrent
writes, "no log no action", tamper-evident Merkle anchoring.
"""

from __future__ import annotations

import threading

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from darwin_dna.machinery.audit_logger import AuditError, AuditLogger
from darwin_fakes.fake_platform import MemoryAnchorStore, MemoryAuditSink


def test_write_before_act_and_outcome_pairing():
    sink = MemoryAuditSink()
    log = AuditLogger("d", sink)
    iid = log.log_intent("payment", {"amount": 1})
    log.log_outcome(iid, "ok", {"tx": "0x1"})
    kinds = [r["kind"] for r in sink.read_all()]
    assert kinds == ["intent:payment", "outcome"]
    log.verify()  # unbroken


def test_chain_survives_restart():
    sink = MemoryAuditSink()
    a = AuditLogger("d", sink)
    a.log_event("birth", {})
    a.log_intent("x", {})
    # "restart": a brand-new logger over the SAME durable sink must resume
    # the same chain, not fork a new one.
    b = AuditLogger("d", sink)
    b.log_event("after_restart", {})
    b.verify()
    seqs = [r["seq"] for r in sink.read_all()]
    assert seqs == list(range(len(seqs)))


def test_tamper_is_detected_on_verify():
    sink = MemoryAuditSink()
    a = AuditLogger("d", sink)
    a.log_event("birth", {})
    a.log_event("spend", {"amount": 5})
    sink._records[1]["payload"]["amount"] = 9999  # rewrite history
    with pytest.raises(AuditError, match="hash mismatch"):
        AuditLogger("d", sink)  # re-verifies on construction


def test_sink_failure_halts_the_darwin():
    class ExplodingSink(MemoryAuditSink):
        def append(self, record):
            raise RuntimeError("disk gone")

    log = AuditLogger("d", MemoryAuditSink())
    log._sink = ExplodingSink()
    with pytest.raises(AuditError):
        log.log_intent("payment", {})  # no log => the action must not happen


def test_concurrent_writes_are_serialized():
    sink = MemoryAuditSink()
    log = AuditLogger("d", sink)

    def worker(n):
        for i in range(50):
            log.log_event("e", {"w": n, "i": i})

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    records = sink.read_all()
    assert len(records) == 400
    assert [r["seq"] for r in records] == list(range(400))
    log.verify()  # the chain linearized correctly under contention


def test_merkle_anchor_mismatch_halts():
    sink = MemoryAuditSink()
    store = MemoryAnchorStore()
    log = AuditLogger("d", sink)
    log.log_event("birth", {})
    log.anchor(store, day="2026-01-01")
    # Platform's stored root for that day disagrees with ours now.
    store.put_root("d", "2026-01-01", "deadbeef" * 8)
    with pytest.raises(AuditError, match="Merkle anchor mismatch"):
        log.anchor(store, day="2026-01-01")


@settings(max_examples=60, deadline=None)
@given(st.lists(st.tuples(st.text(max_size=8), st.integers()), max_size=40))
def test_chain_integrity_property(events):
    sink = MemoryAuditSink()
    log = AuditLogger("d", sink)
    for k, v in events:
        log.log_event("e", {"k": k, "v": v})
    log.verify()
    recs = sink.read_all()
    prev = "0" * 64
    for i, r in enumerate(recs):
        assert r["seq"] == i
        assert r["prev_hash"] == prev
        prev = r["hash"]
