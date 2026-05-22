"""Ledger CSV export (docs/engineering/05, 17).

A jurisdiction-neutral export of a Darwin's money movements — date, amount,
direction, counterparty, category. The owner (or their accountant) does the
actual tax filing; the platform only hands over clean records.

Country-specific tax-form generation (US 1099, UK Self Assessment, EU
equivalents) is intentionally **out of scope** — it is jurisdiction-specific,
changes yearly, and a platform should not be a tax preparer.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from typing import Any

_COLUMNS = (
    "seq",
    "timestamp_utc",
    "type",
    "direction",
    "amount_usdc",
    "counterparty",
    "category",
    "memo",
)


def _iso(ts: Any) -> str:
    """A wallet timestamp (epoch seconds) as a UTC ISO-8601 string."""
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError):
        return ""


def _normalise(seq: int, movement: dict[str, Any]) -> dict[str, Any] | None:
    """Flatten one wallet movement — which is keyed by its kind, e.g.
    ``{"debit": {...}}`` — into a flat accounting row. Returns ``None`` for
    an empty / unrecognised movement so the caller can skip it."""
    if not movement:
        return None
    mtype = next(iter(movement))  # "debit" | "credit" | "harvest" | ...
    body = movement.get(mtype)
    if not isinstance(body, dict) or not body:
        return None
    return {
        "seq": seq,
        "timestamp_utc": _iso(body.get("ts")),
        "type": mtype,
        "direction": "in" if mtype == "credit" else "out",
        "amount_usdc": body.get("amount_usdc", ""),
        "counterparty": (
            body.get("to")
            or body.get("from")
            or body.get("destination")
            or ""
        ),
        "category": body.get("kind", mtype),
        "memo": body.get("memo", ""),
    }


def movements_to_csv(movements: list[dict[str, Any]]) -> str:
    """Render a Darwin's wallet movement feed as a CSV string. The first
    line is always the header, so an empty ledger still yields a valid,
    openable file."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for seq, movement in enumerate(movements or []):
        row = _normalise(seq, movement)
        if row is not None:
            writer.writerow(row)
    return buf.getvalue()
