"""Daily Merkle-root anchoring (docs/engineering/22 §"Audit log Merkle anchoring").

The genome's :class:`AuditLogger` is hash-chained per-Darwin (doc 18).
A single Darwin's chain is tamper-evident locally, but the platform's
*claim* that the chain is unbroken is unverifiable without an external
witness. Phase 2 closes that gap:

* Every 24 hours, the platform collects the day's :func:`merkle_root`
  from each Darwin's audit logger.
* The fleet-level root is the Merkle root of the per-Darwin daily
  roots (deterministic ordering by ``darwin_id``).
* The fleet root is published to a configurable :class:`AnchorBackend`
  (a public chain in production; an in-memory store in tests).
* Anyone (user, auditor, regulator) verifies the chain by recomputing
  the per-Darwin roots and the fleet root, then checking the published
  anchor.

The publisher is idempotent on ``day_iso`` — re-running today's anchor
returns the cached anchor receipt rather than double-publishing.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable


GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class DarwinDailyRoot:
    """One Darwin's Merkle root for one day."""

    darwin_id: str
    day_iso: str
    root: str

    def to_json(self) -> dict[str, Any]:
        return {
            "darwin_id": self.darwin_id,
            "day_iso": self.day_iso,
            "root": self.root,
        }


@dataclass(frozen=True)
class FleetAnchor:
    """One day's fleet-wide anchor. ``fleet_root`` is the Merkle root
    over per-Darwin roots; ``anchor_tx`` is the publish-side receipt
    the backend returned (chain tx hash in production)."""

    day_iso: str
    fleet_root: str
    per_darwin_roots: list[DarwinDailyRoot]
    anchor_tx: str
    anchored_at: float

    def to_json(self) -> dict[str, Any]:
        return {
            "day_iso": self.day_iso,
            "fleet_root": self.fleet_root,
            "per_darwin_roots": [r.to_json() for r in self.per_darwin_roots],
            "anchor_tx": self.anchor_tx,
            "anchored_at": self.anchored_at,
        }


@runtime_checkable
class AnchorBackend(Protocol):
    """Where the fleet root gets published. Real impl in production is
    a Base-mainnet contract that stores ``(day_iso, fleet_root)``;
    tests use the in-memory :class:`StubAnchorBackend`."""

    def publish(self, *, day_iso: str, fleet_root: str) -> str:
        """Returns the publish receipt id (tx hash or equivalent)."""
        ...

    def lookup(self, day_iso: str) -> str | None:
        """Returns the previously-published fleet root for ``day_iso``,
        or None when not anchored."""
        ...


# ---------------------------------------------------------------------------
# Stub backends
# ---------------------------------------------------------------------------
class StubAnchorBackend:
    """In-memory anchor backend. Records every publish with a
    deterministic synthetic tx id; useful for tests + the 90-day sim."""

    def __init__(self) -> None:
        self._anchors: dict[str, str] = {}
        self._seq = 0
        self.publish_calls: list[tuple[str, str]] = []

    def publish(self, *, day_iso: str, fleet_root: str) -> str:
        self._seq += 1
        if day_iso in self._anchors:
            # Idempotent at the backend: returning the existing tx so
            # repeat publishes are observable but harmless.
            return f"stub-tx-{day_iso}-replay"
        self._anchors[day_iso] = fleet_root
        tx = f"stub-tx-{day_iso}-{self._seq:08d}"
        self.publish_calls.append((day_iso, fleet_root))
        return tx

    def lookup(self, day_iso: str) -> str | None:
        return self._anchors.get(day_iso)


class ChainAnchorBackend:
    """Real anchor backend wrapping a :class:`ChainClient` (same
    Protocol :mod:`darwin_platform.adapters.wallet` uses). The anchor is
    submitted as a memo-only USDC self-transfer for $0.000001 — the
    cost is the gas fee, well below the doc-22 < $0.01/day budget.

    Production wires a dedicated platform anchor wallet (NOT a Darwin
    wallet) so the per-Darwin caps don't refuse the daily transfer.
    """

    ANCHOR_AMOUNT_USDC = 0.000001

    def __init__(
        self,
        *,
        chain: Any,
        anchor_address: str | None = None,
    ) -> None:
        if not hasattr(chain, "transfer"):
            raise TypeError("ChainAnchorBackend requires a ChainClient-shaped object")
        self._chain = chain
        # The anchor's recipient is the chain's own address by default
        # (self-transfer), which avoids paying out USDC.
        self._to = anchor_address or chain.address()
        self._anchors: dict[str, tuple[str, str]] = {}  # day → (root, tx)

    def publish(self, *, day_iso: str, fleet_root: str) -> str:
        if day_iso in self._anchors:
            return self._anchors[day_iso][1]
        memo = f"darwin-anchor:{day_iso}:{fleet_root}"
        tx = self._chain.transfer(
            to=self._to,
            amount_usdc=self.ANCHOR_AMOUNT_USDC,
            memo=memo,
        )
        tx_hash = str((tx or {}).get("tx_hash", ""))
        self._anchors[day_iso] = (fleet_root, tx_hash)
        return tx_hash

    def lookup(self, day_iso: str) -> str | None:
        v = self._anchors.get(day_iso)
        return v[0] if v else None


# ---------------------------------------------------------------------------
# Core merkle helpers (mirror darwin_dna.machinery.audit_logger._merkle_root
# so the platform stays standalone in case the DNA module is unavailable)
# ---------------------------------------------------------------------------
def merkle_root(leaves: list[str]) -> str:
    """Standard binary Merkle root over hex-string leaves. Lonely nodes
    are promoted (duplicated) — same scheme as the genome's audit logger."""
    if not leaves:
        return GENESIS_HASH
    level = list(leaves)
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256((level[i] + level[i + 1]).encode()).hexdigest()
            for i in range(0, len(level), 2)
        ]
    return level[0]


# ---------------------------------------------------------------------------
# The publisher service
# ---------------------------------------------------------------------------
class MerkleAnchorPublisher:
    """Daily fleet-anchor service.

    Construction:

    ::

        publisher = MerkleAnchorPublisher(backend=ChainAnchorBackend(chain=...))

        # Once a day, the supervisor calls this after the daily cron:
        result = publisher.publish_daily(
            day_iso="2026-05-21",
            per_darwin_roots=[
                DarwinDailyRoot(darwin_id="d-1", day_iso="2026-05-21",
                                root=runtime1.audit.merkle_root()),
                DarwinDailyRoot(darwin_id="d-2", day_iso="2026-05-21",
                                root=runtime2.audit.merkle_root()),
            ],
        )

    Returns the :class:`FleetAnchor`. The publisher caches anchors and
    is idempotent on ``day_iso``.
    """

    def __init__(
        self,
        *,
        backend: AnchorBackend,
        clock: Callable[[], float] = time.time,
        audit_recorder: Callable[[str, str, dict[str, Any]], None] | None = None,
    ) -> None:
        if not isinstance(backend, AnchorBackend):
            raise TypeError("MerkleAnchorPublisher requires an AnchorBackend")
        self._backend = backend
        self._clock = clock
        self._recorder = audit_recorder
        self._anchors: dict[str, FleetAnchor] = {}

    def publish_daily(
        self,
        *,
        day_iso: str,
        per_darwin_roots: list[DarwinDailyRoot],
        actor: str = "platform.anchor",
    ) -> FleetAnchor:
        if day_iso in self._anchors:
            return self._anchors[day_iso]
        # Deterministic ordering: sort by darwin_id so anyone can
        # recompute the fleet root from the same inputs.
        ordered = sorted(per_darwin_roots, key=lambda r: r.darwin_id)
        fleet = merkle_root([r.root for r in ordered])
        try:
            tx = self._backend.publish(day_iso=day_iso, fleet_root=fleet)
        except Exception as exc:
            self._audit("anchor_publish_failed", actor, {
                "day_iso": day_iso, "fleet_root": fleet, "reason": str(exc),
            })
            raise
        anchor = FleetAnchor(
            day_iso=day_iso,
            fleet_root=fleet,
            per_darwin_roots=ordered,
            anchor_tx=tx,
            anchored_at=self._clock(),
        )
        self._anchors[day_iso] = anchor
        self._audit("anchor_published", actor, {
            "day_iso": day_iso, "fleet_root": fleet,
            "anchor_tx": tx, "darwin_count": len(ordered),
        })
        return anchor

    def verify_anchor(self, day_iso: str) -> bool:
        """Round-trip check: the cached anchor's fleet root matches what
        the backend returned for that day. Used by ops on a suspicion
        of tampering."""
        cached = self._anchors.get(day_iso)
        if cached is None:
            return False
        backend_root = self._backend.lookup(day_iso)
        return backend_root == cached.fleet_root

    def anchors(self) -> list[FleetAnchor]:
        return list(self._anchors.values())

    def get_anchor(self, day_iso: str) -> FleetAnchor | None:
        return self._anchors.get(day_iso)

    # -- internals ---------------------------------------------------------
    def _audit(self, action: str, actor: str, metadata: dict[str, Any]) -> None:
        if self._recorder is None:
            return
        try:
            self._recorder(actor, action, metadata)
        except Exception:
            pass
