"""Disk-backed append-only audit sink for the per-Darwin orchestrator pod.

The genome's :class:`darwin_dna.machinery.audit_logger.AuditLogger` writes
every intent/outcome/event here. In production the ``AuditSink`` Protocol is a
"durable append-only Postgres table"; the in-memory :class:`MemoryAuditSink`
the test fakes ship is fine for a bounded 90-day sim but pins the genome's
*entire* hash-chained history in RAM — which is exactly the orchestrator pod
leak (an immortal genome appends ~7 records/heartbeat forever).

This sink keeps the durable record on disk (a JSON-lines file on the pod's
node-backed volume, NOT counted against the container memory limit) and holds
only O(1) state in RAM. The genome's per-heartbeat path only ``append``s; the
chain is materialised lazily and *streamed* (boot verify, the daily Merkle
anchor) so memory stays flat for the whole life of the pod.

Crash-safety: ``append`` writes one line and flushes. A pod killed mid-write
can leave a torn final line; readers skip a trailing line that does not parse
or lacks the chain fields, and the genome resumes the chain from the last
intact record (write-before-act means the matching side effect also may not
have completed).
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import deque
from pathlib import Path
from typing import Any, Iterator

GENESIS_HASH = "0" * 64


def _is_record(obj: Any) -> bool:
    return (
        isinstance(obj, dict)
        and "seq" in obj
        and "hash" in obj
        and "prev_hash" in obj
    )


class JsonlAuditSink:
    """Append-only audit sink backed by a JSON-lines file.

    Satisfies the genome's ``AuditSink`` protocol (``append`` + ``read_all``)
    and adds streaming helpers (``iter_records``, ``iter_hashes``, ``count``,
    ``read_tail``, ``merkle_root``) the :class:`AuditLogger` uses to avoid ever
    materialising the full chain.
    """

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Count intact records already on disk (resume across a container
        # restart continues the SAME chain rather than forking a new one).
        self._count = 0
        if self._path.exists():
            for _ in self._iter_raw():
                self._count += 1
        # Line-buffered append handle, opened once and kept for the life of
        # the pod so the per-heartbeat append is a single buffered write.
        self._fh = open(self._path, "a", encoding="utf-8")

    # -- AuditSink protocol -------------------------------------------------
    def append(self, record: dict[str, Any]) -> None:
        self._fh.write(json.dumps(record, separators=(",", ":"), default=str))
        self._fh.write("\n")
        self._fh.flush()
        self._count += 1

    def read_all(self) -> list[dict[str, Any]]:
        """The full chain as a list. Kept for protocol compatibility +
        rare callers (e.g. an operator export); the hot/daily paths use the
        streaming helpers below instead so they never hold this list."""
        return list(self.iter_records())

    # -- streaming helpers --------------------------------------------------
    def _iter_raw(self) -> Iterator[dict[str, Any]]:
        try:
            with open(self._path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        # Torn final line from a mid-write crash — stop here;
                        # the chain is intact up to the previous record.
                        break
                    if _is_record(obj):
                        yield obj
        except FileNotFoundError:
            return

    def iter_records(self) -> Iterator[dict[str, Any]]:
        return self._iter_raw()

    def iter_hashes(self, upto: int | None = None) -> Iterator[str]:
        for i, rec in enumerate(self._iter_raw()):
            if upto is not None and i >= upto:
                return
            yield str(rec["hash"])

    def count(self) -> int:
        return self._count

    def read_tail(self, limit: int) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        dq: deque[dict[str, Any]] = deque(maxlen=limit)
        for rec in self._iter_raw():
            dq.append(rec)
        return list(dq)

    def merkle_root(self, upto: int | None = None) -> str:
        """Merkle root over the first ``upto`` record hashes (or all when
        ``None``), computed by spilling each reduction level to a temp file so
        memory stays O(1) regardless of chain length. Byte-for-byte identical
        to the genome's classic list-based ``_merkle_root`` (asserted in
        ``test_audit_sink``)."""
        return _merkle_root_spilling(
            self.iter_hashes(upto=upto), tmp_dir=self._path.parent,
        )

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass


def _reduce_to_file(src: Iterator[str], tmp_dir: Path) -> tuple[str, int]:
    """Run one Merkle reduction pass over ``src`` (leaf/level hashes),
    pairing consecutive hashes and duplicating a lonely final node, writing
    the parents to a fresh temp file. Returns (path, parent_count). O(1) RAM:
    only one hash is buffered at a time."""
    fd, name = tempfile.mkstemp(dir=str(tmp_dir), suffix=".merkle")
    count = 0
    prev: str | None = None
    have = False
    with os.fdopen(fd, "w", encoding="utf-8") as out:
        for h in src:
            if not have:
                prev, have = h, True
            else:
                out.write(hashlib.sha256((prev + h).encode()).hexdigest())
                out.write("\n")
                count += 1
                have = False
        if have:
            out.write(hashlib.sha256((prev + prev).encode()).hexdigest())
            out.write("\n")
            count += 1
    return name, count


def _iter_file_lines(path: str) -> Iterator[str]:
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield line


def _merkle_root_spilling(leaves: Iterator[str], *, tmp_dir: Path) -> str:
    """Disk-spilling level-by-level Merkle root. Identical output to the
    in-memory form, O(1) RAM (each level lives in a temp file). Temp files are
    always cleaned up."""
    import itertools

    it = iter(leaves)
    first = next(it, None)
    if first is None:
        return GENESIS_HASH
    second = next(it, None)
    if second is None:
        # A single leaf is its own root (classic scheme: no self-pairing).
        return first

    paths: list[str] = []
    try:
        cur, count = _reduce_to_file(
            itertools.chain([first, second], it), tmp_dir,
        )
        paths.append(cur)
        while count > 1:
            nxt, count = _reduce_to_file(_iter_file_lines(cur), tmp_dir)
            paths.append(nxt)
            cur = nxt
        # The single remaining line is the root.
        return next(_iter_file_lines(cur))
    finally:
        for p in paths:
            try:
                os.unlink(p)
            except OSError:
                pass


class FileCheckpointStore:
    """The genome's latest node-boundary checkpoint as one JSON file,
    replaced atomically — the pod's durable twin of ``MemoryCheckpointStore``
    (``/data`` is the pod's PVC), so a restarted pod resumes the same life
    instead of booting fresh."""

    def __init__(self, path: str | os.PathLike) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.saves = 0

    def save(self, darwin_id: str, node: str, state: dict) -> None:
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(
            {"darwin_id": darwin_id, "node": node, "state": state},
            # OrchestratorState dumps may hold frozensets (required_tags).
            default=lambda o: sorted(o) if isinstance(o, (set, frozenset)) else str(o),
        ))
        os.replace(tmp, self._path)
        self.saves += 1

    def load(self, darwin_id: str) -> dict | None:
        if not self._path.exists():
            return None
        rec = json.loads(self._path.read_text())
        if rec.get("darwin_id") != darwin_id:
            return None  # another organism's checkpoint: never resume it
        return {"node": rec["node"], "state": rec["state"]}
