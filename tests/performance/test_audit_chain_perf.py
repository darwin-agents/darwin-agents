"""Audit chain (doc 02 hot path; disk sink for k8s pods).

An immortal Darwin appends ~7 records per heartbeat forever, so the chain's
operations must stay flat in memory (boot verify, Merkle anchor) and flat in
time where they only need recent history (the timeline tail)."""

from __future__ import annotations

import pytest

from darwin_dna.machinery.audit_logger import AuditLogger
from darwin_platform.runtime.audit_sink import JsonlAuditSink

SMALL, LARGE = 2_000, 20_000


@pytest.fixture(scope="module")
def chains(tmp_path_factory):
    """Two on-disk chains, SMALL and LARGE records long."""
    out = {}
    for n in (SMALL, LARGE):
        path = tmp_path_factory.mktemp("audit") / f"chain-{n}.jsonl"
        log = AuditLogger("perf", JsonlAuditSink(path))
        for i in range(n):
            log.log_event("perf", {"i": i})
        out[n] = path
    return out


def test_append_throughput(tmp_path, perf):
    log = AuditLogger("perf", JsonlAuditSink(tmp_path / "a.jsonl"))
    n = 5_000
    elapsed = perf.best_of(lambda: [log.log_event("perf", {"i": i}) for i in range(n)], repeat=1)
    # ~100k/s on a laptop; the budget is 5k/s (write-before-act + fsync-free flush).
    assert elapsed < perf.budget(n / 5_000), f"{n / elapsed:,.0f} appends/s"


def test_boot_memory_is_flat_in_chain_length(chains, perf):
    small = perf.peak_bytes(lambda: AuditLogger("perf", JsonlAuditSink(chains[SMALL])))
    large = perf.peak_bytes(lambda: AuditLogger("perf", JsonlAuditSink(chains[LARGE])))
    # 10× the records must not mean 10× the RAM: boot streams the chain.
    assert large < 2 * small + 256 * 1024, f"boot peak {small:,} B → {large:,} B"


def test_boot_verify_is_linear_not_quadratic(chains, perf):
    small = perf.best_of(lambda: AuditLogger("perf", JsonlAuditSink(chains[SMALL])), repeat=3)
    large = perf.best_of(lambda: AuditLogger("perf", JsonlAuditSink(chains[LARGE])), repeat=3)
    assert large / small < 20, f"10× chain cost {large / small:.1f}× (quadratic would be ~100×)"
    assert large < perf.budget(10.0), f"booting {LARGE:,} records took {large:.2f}s"


def test_merkle_anchor_memory_is_flat(chains, perf):
    small_log = AuditLogger("perf", JsonlAuditSink(chains[SMALL]))
    large_log = AuditLogger("perf", JsonlAuditSink(chains[LARGE]))
    small = perf.peak_bytes(small_log.merkle_root)
    large = perf.peak_bytes(large_log.merkle_root)
    assert large < 2 * small + 256 * 1024, f"merkle peak {small:,} B → {large:,} B"


def test_timeline_tail_cost_is_independent_of_chain_length(chains, perf):
    """The pod's events endpoint reads a bounded tail on every fetch; it must
    cost O(limit), not O(chain) (the k8s timeline polls it continuously)."""
    small_sink, large_sink = JsonlAuditSink(chains[SMALL]), JsonlAuditSink(chains[LARGE])
    small = perf.best_of(lambda: small_sink.read_tail(100), repeat=10)
    large = perf.best_of(lambda: large_sink.read_tail(100), repeat=10)
    assert large < 3 * small + 0.002, f"tail(100): {small * 1e3:.2f} ms → {large * 1e3:.2f} ms"
    assert large < perf.budget(0.05)
