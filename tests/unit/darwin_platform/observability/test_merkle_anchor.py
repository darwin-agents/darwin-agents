"""Tests for the daily Merkle anchor publisher (docs/engineering/22).

Coverage:
- merkle_root: lonely-node promotion + deterministic ordering.
- StubAnchorBackend records publish + returns lookup.
- ChainAnchorBackend submits a tx and caches the day.
- Publisher publish_daily is idempotent on day_iso.
- Publisher orders per-Darwin roots by id so anyone can recompute.
- verify_anchor round-trips against the backend.
"""

from __future__ import annotations

import pytest

from darwin_platform.observability.merkle_anchor import (
    ChainAnchorBackend,
    DarwinDailyRoot,
    MerkleAnchorPublisher,
    StubAnchorBackend,
    merkle_root,
)


# ---------------------------------------------------------------------------
# merkle_root helper
# ---------------------------------------------------------------------------

def test_merkle_root_empty_returns_genesis():
    assert merkle_root([]) == "0" * 64


def test_merkle_root_single_leaf_equals_leaf():
    leaf = "a" * 64
    # Single leaf: still goes through hashing because the lonely-node
    # promotion only triggers above 1; the algorithm short-circuits.
    assert merkle_root([leaf]) == leaf


def test_merkle_root_deterministic_for_pair():
    a = "1" * 64
    b = "2" * 64
    r1 = merkle_root([a, b])
    r2 = merkle_root([a, b])
    assert r1 == r2
    # Order matters: swapping yields a different root.
    assert merkle_root([b, a]) != r1


# ---------------------------------------------------------------------------
# StubAnchorBackend
# ---------------------------------------------------------------------------

def test_stub_backend_publishes_and_looks_up():
    b = StubAnchorBackend()
    tx = b.publish(day_iso="2026-05-21", fleet_root="abc")
    assert tx.startswith("stub-tx-")
    assert b.lookup("2026-05-21") == "abc"
    # Re-publish for the same day is idempotent at the backend.
    tx2 = b.publish(day_iso="2026-05-21", fleet_root="abc")
    assert tx2.endswith("replay")


# ---------------------------------------------------------------------------
# ChainAnchorBackend (with stub chain)
# ---------------------------------------------------------------------------

class _FakeChain:
    def __init__(self):
        self.transfers = []

    def address(self) -> str:
        return "0xplatform-anchor"

    def transfer(self, *, to, amount_usdc, memo):
        h = f"0xtx{len(self.transfers):08x}"
        self.transfers.append({"to": to, "amount_usdc": amount_usdc,
                               "memo": memo, "tx_hash": h})
        return {"tx_hash": h}


def test_chain_backend_submits_memo_only_tx():
    chain = _FakeChain()
    b = ChainAnchorBackend(chain=chain)
    tx = b.publish(day_iso="2026-05-21", fleet_root="def")
    assert tx.startswith("0xtx")
    # Memo carries the anchor — anyone can recover it from the chain log.
    assert "darwin-anchor:2026-05-21" in chain.transfers[0]["memo"]


def test_chain_backend_rejects_non_chain_object():
    with pytest.raises(TypeError, match="ChainClient"):
        ChainAnchorBackend(chain="not a chain")


# ---------------------------------------------------------------------------
# MerkleAnchorPublisher
# ---------------------------------------------------------------------------

def test_publisher_publishes_fleet_root():
    backend = StubAnchorBackend()
    pub = MerkleAnchorPublisher(backend=backend)
    roots = [
        DarwinDailyRoot(darwin_id="d-2", day_iso="2026-05-21", root="bbb"),
        DarwinDailyRoot(darwin_id="d-1", day_iso="2026-05-21", root="aaa"),
    ]
    anchor = pub.publish_daily(day_iso="2026-05-21", per_darwin_roots=roots)
    assert anchor.anchor_tx.startswith("stub-tx-")
    # Per-Darwin roots are ordered by id so anyone can recompute the fleet root.
    assert [r.darwin_id for r in anchor.per_darwin_roots] == ["d-1", "d-2"]
    # The fleet root is the merkle root of the sorted leaves.
    expected = merkle_root(["aaa", "bbb"])
    assert anchor.fleet_root == expected


def test_publisher_idempotent_on_day_iso():
    backend = StubAnchorBackend()
    pub = MerkleAnchorPublisher(backend=backend)
    roots = [DarwinDailyRoot(darwin_id="d-1", day_iso="2026-05-21", root="a")]
    a = pub.publish_daily(day_iso="2026-05-21", per_darwin_roots=roots)
    b = pub.publish_daily(day_iso="2026-05-21", per_darwin_roots=roots)
    assert a is b


def test_verify_anchor_round_trips():
    backend = StubAnchorBackend()
    pub = MerkleAnchorPublisher(backend=backend)
    pub.publish_daily(
        day_iso="2026-05-21",
        per_darwin_roots=[
            DarwinDailyRoot(darwin_id="d-1", day_iso="2026-05-21", root="a"),
        ],
    )
    assert pub.verify_anchor("2026-05-21") is True
    assert pub.verify_anchor("2099-01-01") is False  # never published


def test_publisher_audit_recorder():
    backend = StubAnchorBackend()
    recorded = []
    pub = MerkleAnchorPublisher(
        backend=backend,
        audit_recorder=lambda actor, action, meta: recorded.append(
            (actor, action, meta),
        ),
    )
    pub.publish_daily(
        day_iso="2026-05-21",
        per_darwin_roots=[
            DarwinDailyRoot(darwin_id="d-1", day_iso="2026-05-21", root="a"),
        ],
    )
    actions = [r[1] for r in recorded]
    assert "anchor_published" in actions
