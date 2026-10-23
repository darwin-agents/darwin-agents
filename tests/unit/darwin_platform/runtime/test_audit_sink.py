"""Disk-backed audit sink — the orchestrator pod's bounded-memory audit log.

Covers the round-trip + streaming helpers, exact-Merkle equivalence with the
genome's in-memory form, crash-torn-line tolerance, and full AuditLogger
parity (verify / anchor / re-anchor / resume) against the in-memory fake.
"""

from __future__ import annotations

import hashlib

import pytest

from darwin_dna.machinery.audit_logger import AuditLogger, _merkle_root
from darwin_fakes import MemoryAuditSink
from darwin_platform.runtime.audit_sink import (
    GENESIS_HASH,
    JsonlAuditSink,
    _merkle_root_spilling,
)


class _Store:
    def __init__(self) -> None:
        self._d: dict = {}

    def put_root(self, darwin_id, day, root):
        self._d[(darwin_id, day)] = root

    def get_root(self, darwin_id, day):
        return self._d.get((darwin_id, day))


def _h(i: int) -> str:
    return hashlib.sha256(str(i).encode()).hexdigest()


def test_append_read_count_roundtrip(tmp_path):
    sink = JsonlAuditSink(tmp_path / "a.jsonl")
    recs = [
        {"seq": i, "prev_hash": "0" * 64, "hash": _h(i), "kind": "event:x",
         "ts": float(i), "payload": {"i": i}}
        for i in range(5)
    ]
    for r in recs:
        sink.append(r)
    assert sink.count() == 5
    assert sink.read_all() == recs
    assert list(sink.iter_hashes()) == [_h(i) for i in range(5)]
    assert sink.read_tail(2) == recs[-2:]
    assert sink.read_tail(0) == []


@pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 7, 8, 9, 16, 17, 100, 257, 1000])
def test_merkle_matches_inmemory(tmp_path, n):
    leaves = [_h(i) for i in range(n)]
    assert _merkle_root_spilling(iter(leaves), tmp_dir=tmp_path) == _merkle_root(leaves)


def test_merkle_root_via_sink_matches_and_prefix(tmp_path):
    sink = JsonlAuditSink(tmp_path / "a.jsonl")
    leaves = [_h(i) for i in range(50)]
    for i, h in enumerate(leaves):
        sink.append({"seq": i, "prev_hash": "0" * 64, "hash": h,
                     "kind": "event:x", "ts": 0.0, "payload": {}})
    assert sink.merkle_root() == _merkle_root(leaves)
    assert sink.merkle_root(upto=10) == _merkle_root(leaves[:10])
    assert sink.merkle_root(upto=0) == GENESIS_HASH


def test_torn_final_line_is_tolerated(tmp_path):
    path = tmp_path / "a.jsonl"
    sink = JsonlAuditSink(path)
    sink.append({"seq": 0, "prev_hash": "0" * 64, "hash": _h(0),
                 "kind": "event:x", "ts": 0.0, "payload": {}})
    sink.close()
    # Simulate a crash mid-write: append a partial (unterminated/!JSON) line.
    with open(path, "a", encoding="utf-8") as fh:
        fh.write('{"seq": 1, "prev_hash": "00')  # torn — no newline, invalid
    reopened = JsonlAuditSink(path)
    assert reopened.count() == 1  # the torn record is skipped
    assert [r["seq"] for r in reopened.read_all()] == [0]


def _drive(sink, n=40):
    log = AuditLogger("d1", sink)
    for k in range(n):
        iid = log.log_intent("neuron.x", {"k": k})
        log.log_outcome(iid, "ok")
        log.log_event("heartbeat", {"k": k})
    return log


def test_auditlogger_over_disk_sink_verifies_and_anchors(tmp_path):
    log = _drive(JsonlAuditSink(tmp_path / "a.jsonl"))
    log.verify()  # streamed verify, must pass
    assert log.dispatch_outcome_counts() == {"ok": 40, "fail": 0}
    store = _Store()
    root = log.anchor(store, "2026-01-01")
    assert len(root) == 64
    # Anchoring appends a `merkle_anchor` record, so a same-day re-anchor sees
    # legitimate append-only growth: it must NOT raise (the streamed prefix
    # tamper-check reproduces the prior witnessed checkpoint) and yields a new
    # root over the longer chain.
    root2 = log.anchor(store, "2026-01-01")
    assert len(root2) == 64


def test_auditlogger_resumes_chain_from_disk(tmp_path):
    path = tmp_path / "a.jsonl"
    first = _drive(JsonlAuditSink(path))
    # A fresh logger over the same file continues the SAME chain.
    second = AuditLogger("d1", JsonlAuditSink(path))
    assert second._seq == first._seq  # noqa: SLF001
    assert second._head == first._head  # noqa: SLF001
    assert second.dispatch_outcome_counts() == first.dispatch_outcome_counts()
    second.verify()


def test_tamper_on_disk_is_caught(tmp_path):
    path = tmp_path / "a.jsonl"
    _drive(JsonlAuditSink(path), n=5)
    lines = path.read_text().splitlines()
    # Rewrite one record's payload without fixing its hash → tamper.
    import json
    rec = json.loads(lines[3])
    rec["payload"] = {"tampered": True}
    lines[3] = json.dumps(rec, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n")
    from darwin_dna.machinery.audit_logger import AuditError
    with pytest.raises(AuditError):
        AuditLogger("d1", JsonlAuditSink(path))  # boot verify catches it


def test_parity_with_inmemory_sink_same_inputs(tmp_path, monkeypatch):
    # Pin time so both sinks hash identical records → identical chains.
    import darwin_dna.machinery.audit_logger as al
    ticks = iter(float(i) for i in range(100000))
    monkeypatch.setattr(al.time, "time", lambda: next(ticks))
    mem = _drive(MemoryAuditSink())
    ticks2 = iter(float(i) for i in range(100000))
    monkeypatch.setattr(al.time, "time", lambda: next(ticks2))
    disk = _drive(JsonlAuditSink(tmp_path / "a.jsonl"))
    assert mem._head == disk._head  # noqa: SLF001
    assert mem.merkle_root() == disk.merkle_root()


async def test_file_checkpoint_resumes_the_same_life(tmp_path):
    """A pod restart boots the same DNA + this file and continues where it
    left off; a checkpoint from another organism is never resumed."""
    from darwin_dna.evolution import boot
    from darwin_fakes import FakeStack
    from darwin_platform.runtime.audit_sink import FileCheckpointStore

    path = tmp_path / "checkpoint.json"
    first = FakeStack(opening_usdc=50.0, clock=lambda: 0.0)
    first.checkpoint = FileCheckpointStore(path)
    rt = boot(first.platform(), sleep=lambda _: None)
    await rt.run(max_heartbeats=3)
    tick = rt.state.tick
    assert tick >= 3

    again = FakeStack(opening_usdc=50.0, clock=lambda: 0.0)
    again.manifest, again.layer_blobs, again.layer_names = (
        first.manifest, first.layer_blobs, first.layer_names,
    )
    again.signer, again.identity = first.signer, None
    again.checkpoint = FileCheckpointStore(path)
    assert boot(again.platform(), sleep=lambda _: None).state.tick == tick
    assert FileCheckpointStore(path).load("someone-else") is None
