"""Operator audit log (docs/engineering/08).

Doc 08: "Every endpoint writes to the audit log before mutating state.
The supervisor is the single writer for Darwin lifecycle transitions;
nothing else has that grant." And: "Operator actions also write to the
audit log, with operator identity (verified via WebAuthn-protected
internal tooling). [...] The audit log is structurally separate from
traces. Traces are sampled and rotated; the audit log is permanent and
hash-chained. We do not delete audit entries even when a Darwin dies."

This module is the platform-side hash-chained log of operator actions:

- **Separate from the genome's audit chain** (which is per-Darwin, lives
  in the Darwin's wallet/ledger neighbourhood, and is the thing a
  graceful death anchors). Operator audit records *who-did-what to the
  platform*: which operator paused/quarantined/throttled/set-policy on
  which Darwin, when.
- **Separate from OpenTelemetry spans** (which are sampled and rotated
  for observability). The audit log is durable, append-only, and
  walkable as a hash chain for tamper detection.
- **Genesis hash** = "0" * 64 so an empty log's first append has a
  well-defined prior. Each subsequent entry's ``prev_hash`` is the
  previous entry's ``entry_hash``; ``entry_hash`` is sha256 over the
  entry's canonical body. Tampering with any field of any entry breaks
  the chain at that point — ``verify()`` walks the whole log and
  refuses on the first mismatch.

In production this would be backed by an append-only store (Postgres
with a `seq SERIAL`, S3 Object Lock, or a managed audit service);
the current in-memory implementation is the dev/test default and slots
behind the same surface a real impl would expose.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from typing import Any, Iterable

from darwin_dna._canonical import canonical_bytes


_GENESIS_HASH = "0" * 64


@dataclass(frozen=True)
class OperatorAuditEntry:
    """One operator action. Frozen so a stored entry cannot be mutated
    after the chain hashes over it."""

    seq: int
    ts: float
    actor: str  # subject from the auth context (email / user id / "anonymous")
    action: str  # "spawn" | "pause" | "kill" | ...
    darwin_id: str | None
    metadata: dict[str, Any]
    prev_hash: str
    entry_hash: str

    def body_bytes(self) -> bytes:
        """The bytes that are hashed for ``entry_hash``. Excludes the hash
        itself so verification recomputes a stable digest."""
        return _entry_body_bytes(
            seq=self.seq,
            ts=self.ts,
            actor=self.actor,
            action=self.action,
            darwin_id=self.darwin_id,
            metadata=self.metadata,
            prev_hash=self.prev_hash,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "actor": self.actor,
            "action": self.action,
            "darwin_id": self.darwin_id,
            "metadata": dict(self.metadata),
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


def _entry_body_bytes(
    *,
    seq: int,
    ts: float,
    actor: str,
    action: str,
    darwin_id: str | None,
    metadata: dict[str, Any],
    prev_hash: str,
) -> bytes:
    return canonical_bytes(
        {
            "seq": int(seq),
            "ts": float(ts),
            "actor": str(actor),
            "action": str(action),
            "darwin_id": darwin_id,
            "metadata": dict(metadata or {}),
            "prev_hash": str(prev_hash),
        }
    )


class OperatorAuditError(Exception):
    """A chain mismatch — typically tamper detection or a misuse of
    the API (e.g. mutating a stored entry)."""


class OperatorAuditLog:
    """Append-only hash-chained log of operator actions. Construct one
    per supervisor process (or per tenant in a multi-tenant fleet)."""

    def __init__(self, *, clock=time.time) -> None:
        self._entries: list[OperatorAuditEntry] = []
        self._clock = clock

    # -- writes -----------------------------------------------------------
    def record(
        self,
        *,
        actor: str,
        action: str,
        darwin_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> OperatorAuditEntry:
        """Append one entry and return it. Hashes link every entry to the
        previous, with a fixed genesis hash for the first."""
        # The actor MUST be a non-empty string. An empty actor means the
        # caller forgot to thread auth — fail loudly rather than write a
        # field that's meaningless for compliance.
        actor = str(actor) if actor else ""
        if not actor:
            raise OperatorAuditError("actor is required for an audit entry")
        action = str(action)
        if not action:
            raise OperatorAuditError("action is required for an audit entry")

        seq = len(self._entries)
        ts_val = float(ts) if ts is not None else float(self._clock())
        prev_hash = (
            self._entries[-1].entry_hash if self._entries else _GENESIS_HASH
        )
        meta = dict(metadata or {})
        body = _entry_body_bytes(
            seq=seq,
            ts=ts_val,
            actor=actor,
            action=action,
            darwin_id=darwin_id,
            metadata=meta,
            prev_hash=prev_hash,
        )
        entry_hash = hashlib.sha256(body).hexdigest()
        entry = OperatorAuditEntry(
            seq=seq,
            ts=ts_val,
            actor=actor,
            action=action,
            darwin_id=darwin_id,
            metadata=meta,
            prev_hash=prev_hash,
            entry_hash=entry_hash,
        )
        self._entries.append(entry)
        return entry

    # -- reads ------------------------------------------------------------
    def entries(
        self,
        *,
        actor: str | None = None,
        action: str | None = None,
        darwin_id: str | None = None,
        since: float | None = None,
        limit: int | None = None,
    ) -> list[OperatorAuditEntry]:
        """Filtered view. Filters compose; ``limit`` returns the most
        recent N matching entries."""
        rows = list(self._entries)
        if actor is not None:
            rows = [e for e in rows if e.actor == actor]
        if action is not None:
            rows = [e for e in rows if e.action == action]
        if darwin_id is not None:
            rows = [e for e in rows if e.darwin_id == darwin_id]
        if since is not None:
            rows = [e for e in rows if e.ts >= since]
        if limit is not None:
            rows = rows[-int(limit):]
        return rows

    def all(self) -> list[OperatorAuditEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # -- chain integrity --------------------------------------------------
    def verify(self) -> None:
        """Walk the chain. Raises :class:`OperatorAuditError` on the first
        mismatch (genesis, link, or hash). Pure; no I/O."""
        prev = _GENESIS_HASH
        for i, e in enumerate(self._entries):
            if e.seq != i:
                raise OperatorAuditError(
                    f"entry seq {e.seq} at index {i}: out of order"
                )
            if e.prev_hash != prev:
                raise OperatorAuditError(
                    f"entry {e.seq}: prev_hash mismatch "
                    f"(expected {prev}, got {e.prev_hash})"
                )
            expected = hashlib.sha256(e.body_bytes()).hexdigest()
            if e.entry_hash != expected:
                raise OperatorAuditError(
                    f"entry {e.seq}: entry_hash mismatch (tampered?)"
                )
            prev = e.entry_hash

    def tip_hash(self) -> str:
        """The most recent entry's hash, or the genesis sentinel."""
        return self._entries[-1].entry_hash if self._entries else _GENESIS_HASH


def to_dicts(entries: Iterable[OperatorAuditEntry]) -> list[dict[str, Any]]:
    """Helper for API serialisation. Bulk converts entries to dicts."""
    return [e.to_dict() for e in entries]
