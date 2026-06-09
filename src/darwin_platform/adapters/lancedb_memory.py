"""LanceDB-backed episodic memory (docs/engineering/03, 11, 19).

The genome ships a *deterministic* episodic memory — exact-signature +
token-overlap recall (`darwin_dna.machinery.memory.EpisodicMemory`). That
stays the default: the 90-day sim and cross-engine parity depend on it,
and embeddings cannot live in the DNA (model lock-in P6, heavy dep P9).

This is the production *substrate*: a per-Darwin LanceDB vector store the
platform injects in place of the deterministic default, giving genuine
semantic recall ("have I seen something *like* this?"). It implements the
exact same surface — ``record`` / ``recall`` / ``all`` / ``__len__`` — so
``DarwinRuntime`` uses it unchanged when ``platform.episodic_memory`` is
set.

``embed`` is injected — a ``callable(text) -> sequence[float]``. Tests
pass a deterministic fake; production passes a real embedding model.
LanceDB is embedded (a local directory) — no server, no network.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Callable

from darwin_dna.machinery.memory import EpisodicRecord

_TABLE = "episodic"


class LanceDbEpisodicMemory:
    """Episodic memory backed by a per-Darwin LanceDB table. Same surface
    as the genome's :class:`EpisodicMemory`; recall is vector similarity
    over the embedded work-signature (an identical signature embeds to the
    same vector, so an exact prior still ranks first — for free)."""

    def __init__(
        self,
        *,
        embed: Callable[[str], Sequence[float]],
        db_path: str,
        table_name: str = _TABLE,
    ) -> None:
        if embed is None:
            raise ValueError("LanceDbEpisodicMemory requires an embed callable")
        if not db_path:
            raise ValueError("LanceDbEpisodicMemory requires a db_path")
        self._embed = embed
        self._table_name = table_name
        try:
            import lancedb  # type: ignore[import-not-found]
        except ImportError as exc:  # pragma: no cover - operator hint
            raise ImportError(
                "LanceDB episodic memory requires the [lancedb] extra: "
                "pip install 'darwin-dna[lancedb]'"
            ) from exc
        self._db = lancedb.connect(db_path)
        # Table is created lazily on the first record so the schema (and
        # the embedding dimension) is inferred from a real row.
        # lancedb >= 0.13 returns a ``ListTablesResponse`` from
        # ``list_tables()`` whose ``in`` operator does NOT check the names
        # (it iterates fields like a NamedTuple). Older releases returned a
        # plain ``list[str]``; ``getattr(..., "tables", listed)`` handles
        # both shapes.
        _listed = self._db.list_tables()
        _names = getattr(_listed, "tables", _listed)
        self._table: Any = (
            self._db.open_table(table_name) if table_name in _names else None
        )

    # -- EpisodicMemory surface -------------------------------------------
    def record(
        self,
        *,
        tick: int,
        signature: str,
        neuron: str,
        outcome: str,
        error_code: str | None = None,
        detail: dict[str, Any] | None = None,
        ts: float | None = None,
    ) -> None:
        import time

        # The embedder bills the Darwin's wallet and fails closed when the
        # budget is exhausted (a refused debit or a proxy 429). Episodic memory
        # is a best-effort substrate, so a budget-failed (or transient) embed
        # SKIPS the write rather than crashing the heartbeat — a Darwin that
        # can't pay simply stops growing its memory until the solvency gate
        # reaps it.
        try:
            vector = [float(x) for x in self._embed(signature)]
        except Exception:
            return

        row = {
            "vector": vector,
            "tick": int(tick),
            "ts": time.time() if ts is None else float(ts),
            "signature": signature,
            "neuron": neuron,
            "outcome": outcome,
            "error_code": error_code or "",
            "detail_json": json.dumps(detail or {}),
        }
        if self._table is None:
            self._table = self._db.create_table(self._table_name, data=[row])
        else:
            self._table.add([row])

    def recall(
        self, signature: str, *, limit: int = 5,
    ) -> list[EpisodicRecord]:
        """Vector-similarity recall, nearest first. An identical signature
        embeds to the same vector, so an exact prior ranks first."""
        if self._table is None or limit <= 0:
            return []
        # As in record(): a budget-failed/transient embed degrades to an empty
        # recall rather than crashing the planner.
        try:
            query = [float(x) for x in self._embed(signature)]
        except Exception:
            return []
        rows = self._table.search(query).limit(limit).to_list()
        return [_to_record(r) for r in rows]

    def all(self) -> list[EpisodicRecord]:
        if self._table is None:
            return []
        rows = self._table.to_arrow().to_pylist()
        return [_to_record(r) for r in rows]

    def __len__(self) -> int:
        return 0 if self._table is None else int(self._table.count_rows())


def _to_record(row: dict[str, Any]) -> EpisodicRecord:
    """Reconstruct an :class:`EpisodicRecord` from a LanceDB row, dropping
    LanceDB's own ``vector`` / ``_distance`` columns."""
    try:
        detail = json.loads(row.get("detail_json") or "{}")
    except (TypeError, ValueError):
        detail = {}
    return EpisodicRecord(
        tick=int(row.get("tick", 0)),
        ts=float(row.get("ts", 0.0)),
        signature=str(row.get("signature", "")),
        neuron=str(row.get("neuron", "")),
        outcome=str(row.get("outcome", "")),
        error_code=(row.get("error_code") or None),
        detail=detail if isinstance(detail, dict) else {},
    )
