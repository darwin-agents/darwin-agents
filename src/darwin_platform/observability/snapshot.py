"""Supervisor state snapshots (docs/engineering/08, write-side).

A JSON dump of every Darwin the supervisor is hosting at the moment of the
call. Used by ops to inspect a running supervisor without going through the
HTTP surface, and by operators investigating an incident.

Restore is deliberately out of scope for this first pass: re-booting a
hosted Darwin from a snapshot requires reconstructing the fake stack's
wallet ledger, audit chain, and signing state, which the genome treats as
authoritative platform-side records. A real platform restores from the
hash-chained audit log + on-chain wallet, not from this dump.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ..runtime.supervisor import Supervisor


def snapshot(supervisor: Supervisor) -> dict[str, Any]:
    """Build a JSON-serializable snapshot of the supervisor's current state.

    Includes per-Darwin status (the same shape the API returns) plus the
    lineage edges the supervisor has observed. Pure read; no flags flipped.
    """
    # Reach into the supervisor's internals deliberately. The Supervisor's
    # public methods are coroutines; building a snapshot from a sync caller
    # would otherwise need an event loop. The fields read here are stable.
    handles = list(supervisor._darwins.values())  # noqa: SLF001
    edges = supervisor._lineage_registry.edges()  # noqa: SLF001
    return {
        "schema": "darwin-platform.snapshot/2",
        "captured_at": time.time(),
        "darwins": [asdict(supervisor._status(h)) for h in handles],  # noqa: SLF001
        "lineage_edges": [
            {
                "parent_id": e.parent_id,
                "child_id": e.child_id,
                "spawned_at": e.spawned_at,
                "budget_usdc": e.budget_usdc,
            }
            for e in edges
        ],
        "alerts": [a.to_dict() for a in supervisor.monitor.all_alerts()],
    }


def write_snapshot(supervisor: Supervisor, path: str | Path) -> Path:
    """Write a snapshot to ``path`` as pretty-printed JSON. Returns the path."""
    p = Path(path)
    p.write_text(json.dumps(snapshot(supervisor), indent=2, sort_keys=True))
    return p
