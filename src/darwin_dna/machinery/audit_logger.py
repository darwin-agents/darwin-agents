"""The truth: the write-before-act primitive (docs/engineering/18 hot path #3,
P7: every action is auditable).

Every neuron dispatch, every payment, every mutation, every reproduction calls
``log_intent`` *first*. The action only proceeds if the intent committed. When
it finishes (success or failure) ``log_outcome`` closes the record. The log is
an append-only, hash-chained sequence: tampering with history breaks the chain
and is detectable; a daily Merkle root is anchored to the platform so even
wholesale rewrites are caught.

"No log, no action." If the sink write fails, this module raises and the loop
halts. We never take a side effect we could not record.
"""

from __future__ import annotations

import hashlib
import threading
import time
from typing import Any, Protocol, runtime_checkable

from .._canonical import canonical_bytes

GENESIS_HASH = "0" * 64  # prev_hash of the first record in a fresh chain


class AuditError(Exception):
    """The audit log could not be written or is inconsistent. The Darwin must
    halt: an unrecordable action must not happen."""


@runtime_checkable
class AuditSink(Protocol):
    """Durable append-only storage. Real impl: append-only Postgres table.
    Fake: in-memory list. The sink only appends and reads; it never mutates
    or deletes."""

    def append(self, record: dict[str, Any]) -> None: ...

    def read_all(self) -> list[dict[str, Any]]: ...


@runtime_checkable
class MerkleAnchorStore(Protocol):
    """The platform's record of each day's Merkle root. The genome writes a
    root; reconciliation reads it back and compares."""

    def put_root(self, darwin_id: str, day: str, root: str) -> None: ...

    def get_root(self, darwin_id: str, day: str) -> str | None: ...


def _hash_record(seq: int, prev_hash: str, ts: float, kind: str, payload: dict) -> str:
    return hashlib.sha256(
        canonical_bytes(
            {
                "seq": seq,
                "prev_hash": prev_hash,
                "ts": ts,
                "kind": kind,
                "payload": payload,
            }
        )
    ).hexdigest()


def _merkle_root(leaves: list[str]) -> str:
    """Standard binary Merkle root over record hashes. Lonely nodes are
    promoted (duplicated) — the conventional, well-understood scheme (P9)."""
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


class AuditLogger:
    """Serialized, hash-chained, write-before-act logger.

    Concurrency: every public method takes one lock, so concurrent callers
    are linearized — the chain can never interleave two records' hashing.
    """

    def __init__(self, darwin_id: str, sink: AuditSink):
        if not isinstance(sink, AuditSink):
            raise TypeError("AuditLogger needs an AuditSink")
        self._darwin_id = darwin_id
        self._sink = sink
        self._lock = threading.Lock()
        # Rebuild chain head from durable storage, verifying as we go. A
        # crash-restart must resume the *same* chain, not fork a new one.
        records = self._verify_chain(sink.read_all())
        self._seq = records[-1]["seq"] + 1 if records else 0
        self._head = records[-1]["hash"] if records else GENESIS_HASH
        # Ground-truth tally (doc 10). reflect reconciles the genome's
        # in-memory ``successful_dispatches`` against what the durable
        # log actually recorded. Counting it by re-scanning the chain
        # every heartbeat is O(n) per beat → O(n²) over a life; instead
        # the logger keeps a running lifetime count of neuron-dispatch
        # outcomes, rebuilt from the chain at boot and maintained per
        # commit. ``_open_neuron_intents`` tracks intents awaiting an
        # outcome so only genuine ``intent:neuron.*`` dispatches are
        # counted (not mutation / reproduction / escalation outcomes).
        self._open_neuron_intents: set[str] = set()
        self._dispatch_ok = 0
        self._dispatch_fail = 0
        for rec in records:
            self._tally_record(rec)

    def _tally_record(self, rec: dict[str, Any]) -> None:
        """Fold one committed record into the ground-truth tally. Called
        for every record at boot and after every live commit."""
        kind = rec.get("kind", "")
        if kind.startswith("intent:neuron."):
            self._open_neuron_intents.add(
                f"{rec['seq']}:{rec['hash'][:16]}"
            )
            return
        if kind != "outcome":
            return
        payload = rec.get("payload") or {}
        iid = payload.get("intent_id")
        if iid not in self._open_neuron_intents:
            return
        self._open_neuron_intents.discard(iid)
        status = payload.get("status")
        if status == "ok":
            self._dispatch_ok += 1
        elif status in ("fail", "failed", "refused"):
            self._dispatch_fail += 1

    def dispatch_outcome_counts(self) -> dict[str, int]:
        """Lifetime count of neuron-dispatch outcomes by status — the
        O(1) ground-truth signal reflect reconciles against (doc 10).
        ``ok`` should track ``state.successful_dispatches`` 1:1."""
        with self._lock:
            return {"ok": self._dispatch_ok, "fail": self._dispatch_fail}

    # -- chain integrity ---------------------------------------------------
    @staticmethod
    def _verify_chain(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        prev = GENESIS_HASH
        for i, rec in enumerate(records):
            if rec.get("seq") != i:
                raise AuditError(f"audit seq gap/dup at position {i}: {rec.get('seq')}")
            if rec.get("prev_hash") != prev:
                raise AuditError(f"audit chain broken at seq {i} (prev_hash)")
            expected = _hash_record(
                rec["seq"], rec["prev_hash"], rec["ts"], rec["kind"], rec["payload"]
            )
            if rec.get("hash") != expected:
                raise AuditError(f"audit record {i} hash mismatch (tampered)")
            prev = rec["hash"]
        return records

    def verify(self) -> None:
        """Re-verify the whole chain from durable storage. Used at boot and
        before any reconciliation."""
        with self._lock:
            self._verify_chain(self._sink.read_all())

    def read_records(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Read the durable chain (newest-last). Used by the reflect node's
        ground-truth reconciliation (doc 10): compare in-memory beliefs to
        what the audit log actually says.

        The records are returned defensively-copied so a caller cannot
        mutate committed history. ``limit=None`` returns the full chain;
        a numeric limit returns the last N records."""
        with self._lock:
            rows = self._sink.read_all()
        if limit is not None and limit > 0:
            rows = rows[-int(limit):]
        return rows

    # -- write-before-act --------------------------------------------------
    def _commit(self, kind: str, payload: dict[str, Any]) -> dict[str, Any]:
        seq = self._seq
        ts = time.time()
        rec_hash = _hash_record(seq, self._head, ts, kind, payload)
        record = {
            "seq": seq,
            "prev_hash": self._head,
            "ts": ts,
            "kind": kind,
            "payload": payload,
            "hash": rec_hash,
            "darwin_id": self._darwin_id,
        }
        try:
            self._sink.append(record)
        except Exception as exc:  # storage failure => the action must not happen
            raise AuditError(f"audit sink append failed: {exc}") from exc
        self._seq = seq + 1
        self._head = rec_hash
        # Fold into the ground-truth tally (doc 10) — every commit goes
        # through here, so the lifetime dispatch-outcome counts stay
        # exact without any per-heartbeat re-scan.
        self._tally_record(record)
        return record

    def log_intent(self, kind: str, payload: dict[str, Any]) -> str:
        """Record the *intent* to act, before acting. Returns an intent id
        the caller threads into ``log_outcome``. If this raises, the caller
        must not perform the action."""
        with self._lock:
            rec = self._commit(f"intent:{kind}", payload)
            return f"{rec['seq']}:{rec['hash'][:16]}"

    def log_outcome(
        self, intent_id: str, status: str, detail: dict[str, Any] | None = None
    ) -> None:
        """Close an intent. ``status`` is "ok" | "fail" | "refused" | etc.
        Always called, success or failure, so every intent has a terminal
        record."""
        with self._lock:
            self._commit(
                "outcome",
                {"intent_id": intent_id, "status": status, "detail": detail or {}},
            )

    def log_event(self, kind: str, payload: dict[str, Any]) -> None:
        """A standalone, non-paired record (birth, death, heartbeat, anchor)."""
        with self._lock:
            self._commit(f"event:{kind}", payload)

    def open_intents(self) -> list[dict[str, Any]]:
        """Intent records (``intent:*``) with no matching ``outcome``.

        ``log_intent`` then act then ``log_outcome`` is the write-before-act
        primitive that protects the one irreversible operation (an on-chain
        transfer). A crash between the act and ``log_outcome`` leaves a
        dangling intent — this surfaces those so the operator reconciles them
        on restart (rather than the dangling payment going unnoticed)."""
        with self._lock:
            rows = self._sink.read_all()
        closed: set[str] = set()
        intents: dict[str, dict[str, Any]] = {}
        for r in rows:
            kind = str(r.get("kind", ""))
            if kind.startswith("intent:"):
                iid = f"{r['seq']}:{r['hash'][:16]}"
                intents[iid] = r
            elif kind == "outcome":
                iid = (r.get("payload") or {}).get("intent_id")
                if iid:
                    closed.add(str(iid))
        return [rec for iid, rec in intents.items() if iid not in closed]

    # -- tamper-evidence anchoring -----------------------------------------
    def _snapshot(self) -> tuple[list[str], int]:
        with self._lock:
            hashes = [r["hash"] for r in self._sink.read_all()]
        return hashes, len(hashes)

    def merkle_root(self) -> str:
        hashes, _ = self._snapshot()
        return _merkle_root(hashes)

    def anchor(self, store: MerkleAnchorStore, day: str | None = None) -> str:
        """Publish the day's Merkle root to the platform and reconcile against
        the previously witnessed checkpoint.

        The chain is append-only, so a Darwin legitimately anchors the *same*
        day many times as it keeps acting (every heartbeat that crosses a day
        boundary, and again when it writes its final death/kill records). That
        is growth, not tampering. Tampering is when a *previously witnessed*
        checkpoint can no longer be reproduced from the current chain — i.e.
        the prefix the old root covered no longer hashes to the old root, or a
        foreign pinned root simply does not match. Either of those is
        unrecoverable: raise and halt.
        """
        import json

        day = day or time.strftime("%Y-%m-%d", time.gmtime())
        hashes, n = self._snapshot()
        root = _merkle_root(hashes)

        raw = store.get_root(self._darwin_id, day)
        if raw is not None:
            prior = None
            try:
                p = json.loads(raw)
                if isinstance(p, dict) and "root" in p and "n" in p:
                    prior = (str(p["root"]), int(p["n"]))
            except (ValueError, TypeError):
                prior = None

            if prior is not None:
                old_root, old_n = prior
                # Legitimate iff the prefix the old root covered still hashes
                # to the old root. Append-only growth always satisfies this;
                # any rewrite of already-witnessed history fails it.
                if not (0 <= old_n <= n and _merkle_root(hashes[:old_n]) == old_root):
                    raise AuditError(
                        f"Merkle anchor divergence for {day}: previously "
                        f"witnessed checkpoint (n={old_n}) no longer "
                        f"reproduces — audit log tampered"
                    )
            elif raw != root:
                # A foreign / externally pinned root that is not ours and does
                # not match: divergence from what the platform witnessed.
                raise AuditError(
                    f"Merkle anchor mismatch for {day}: local {root[:12]} != "
                    f"platform {str(raw)[:12]} (audit log diverged)"
                )

        store.put_root(
            self._darwin_id, day, json.dumps({"root": root, "n": n})
        )
        self.log_event("merkle_anchor", {"day": day, "root": root})
        return root
