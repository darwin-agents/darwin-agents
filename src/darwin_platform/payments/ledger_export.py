"""Ledger export — CSV + jurisdiction-neutral annual summary (docs 05, 17).

A Darwin's money movements, handed to the owner (or their accountant) for
their own tax filing. The platform produces clean records; it does **not**
file taxes or generate any country's statutory return.

Two exports, both jurisdiction-neutral:

- :func:`movements_to_csv` — every movement as a flat CSV row (date,
  amount, direction, counterparty, category, memo). The raw feed.
- :func:`movements_to_summary` — totals for a tax year (or any date
  range): inflow, outflow, net, and a per-category breakdown. The
  numbers an accountant or a tax tool (TurboTax, Koinly, …) consumes,
  with no dependency on any jurisdiction's rules — so it never goes
  stale. :func:`merge_summaries` folds several per-Darwin summaries into
  one owner-level total.

Per-country statutory form generation (US 1099/Schedule C/D, UK Self
Assessment, EU equivalents) is intentionally **out of scope**: it is
jurisdiction-specific, changes yearly, and would make the platform an
(in many places regulated) tax preparer. Every comparable platform
(Coinbase, Stripe, brokerages) hands over records, not filled-in forms.
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


def _amount(value: Any) -> float:
    """A movement amount as a float; non-numeric / missing → 0.0."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _fields(movement: dict[str, Any]) -> dict[str, Any] | None:
    """Pull the accounting-relevant fields out of one wallet movement —
    which is keyed by its kind, e.g. ``{"debit": {...}}``. Returns
    ``None`` for an empty / unrecognised movement so callers can skip it.

    ``ts`` is kept as a raw epoch float (or ``None``) for date filtering;
    ``amount_usdc`` is coerced to a float for arithmetic."""
    if not movement:
        return None
    mtype = next(iter(movement))  # "debit" | "credit" | "harvest" | ...
    body = movement.get(mtype)
    if not isinstance(body, dict) or not body:
        return None
    raw_ts = body.get("ts")
    try:
        ts = float(raw_ts)
    except (TypeError, ValueError):
        ts = None
    return {
        "ts": ts,
        "type": mtype,
        "direction": "in" if mtype == "credit" else "out",
        "amount_usdc": _amount(body.get("amount_usdc")),
        "counterparty": (
            body.get("to")
            or body.get("from")
            or body.get("destination")
            or ""
        ),
        "category": body.get("kind", mtype),
        "memo": body.get("memo", ""),
    }


def movements_to_rows(
    movements: list[dict[str, Any]], *, seq_base: int = 0,
) -> list[dict[str, Any]]:
    """Flat, UI-friendly rows (seq, ts, type, direction, amount_usdc,
    counterparty, category, memo) from raw kind-keyed wallet movements —
    the shape the dashboard's ledger table renders. ``seq_base`` keeps seq
    stable when flattening one page of a larger feed. Unrecognised
    movements are skipped."""
    rows: list[dict[str, Any]] = []
    for i, movement in enumerate(movements or []):
        fields = _fields(movement)
        if fields is not None:
            rows.append({"seq": seq_base + i, **fields})
    return rows


def _csv_row(seq: int, fields: dict[str, Any]) -> dict[str, Any]:
    """A :func:`_fields` dict as a CSV row (raw ts → ISO string)."""
    return {
        "seq": seq,
        "timestamp_utc": _iso(fields["ts"]) if fields["ts"] is not None else "",
        "type": fields["type"],
        "direction": fields["direction"],
        "amount_usdc": fields["amount_usdc"],
        "counterparty": fields["counterparty"],
        "category": fields["category"],
        "memo": fields["memo"],
    }


def movements_to_csv(movements: list[dict[str, Any]]) -> str:
    """Render a Darwin's wallet movement feed as a CSV string. The first
    line is always the header, so an empty ledger still yields a valid,
    openable file."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for seq, movement in enumerate(movements or []):
        fields = _fields(movement)
        if fields is not None:
            writer.writerow(_csv_row(seq, fields))
    return buf.getvalue()


def _year_bounds(year: int) -> tuple[float, float]:
    """[start, end) epoch-second bounds of a calendar (UTC) tax year."""
    start = datetime(year, 1, 1, tzinfo=timezone.utc).timestamp()
    end = datetime(year + 1, 1, 1, tzinfo=timezone.utc).timestamp()
    return start, end


def movements_to_summary(
    movements: list[dict[str, Any]],
    *,
    year: int | None = None,
    start_ts: float | None = None,
    end_ts: float | None = None,
) -> dict[str, Any]:
    """A jurisdiction-neutral accounting summary of a Darwin's movements.

    Scope the period one of three ways (most specific wins):

    - ``year=2024`` — the calendar/UTC year (Jan 1 ≤ ts < Jan 1 next).
    - ``start_ts`` / ``end_ts`` — an explicit half-open epoch range
      ``[start, end)``; either bound may be omitted for open-ended.
    - neither — all movements (lifetime).

    Returns totals (USDC) plus a per-category breakdown. There is no
    tax-rate, cost-basis lot accounting, or country logic here on
    purpose — those are the filer's (or their tool's) job; this is the
    clean input they consume.
    """
    if year is not None:
        start_ts, end_ts = _year_bounds(year)

    total_in = 0.0
    total_out = 0.0
    counted = 0
    skipped_undated = 0
    by_category: dict[str, dict[str, Any]] = {}

    for movement in movements or []:
        fields = _fields(movement)
        if fields is None:
            continue
        ts = fields["ts"]
        # A dated period (year or explicit bound) excludes undated
        # movements — we can't honestly place them in a tax year.
        if start_ts is not None or end_ts is not None:
            if ts is None:
                skipped_undated += 1
                continue
            if start_ts is not None and ts < start_ts:
                continue
            if end_ts is not None and ts >= end_ts:
                continue

        amount = fields["amount_usdc"]
        counted += 1
        if fields["direction"] == "in":
            total_in += amount
        else:
            total_out += amount

        cat = fields["category"]
        bucket = by_category.setdefault(
            cat, {"direction": fields["direction"], "count": 0, "amount_usdc": 0.0}
        )
        bucket["count"] += 1
        bucket["amount_usdc"] = round(bucket["amount_usdc"] + amount, 6)
        if bucket["direction"] != fields["direction"]:
            bucket["direction"] = "mixed"

    summary: dict[str, Any] = {
        "currency": "USDC",
        "period": {
            "year": year,
            "start_utc": _iso(start_ts) if start_ts is not None else None,
            "end_utc": _iso(end_ts) if end_ts is not None else None,
        },
        "movement_count": counted,
        "total_in_usdc": round(total_in, 6),
        "total_out_usdc": round(total_out, 6),
        "net_usdc": round(total_in - total_out, 6),
        "by_category": by_category,
    }
    if skipped_undated:
        # Surfaced, not silently dropped — an accountant needs to know the
        # period total excludes N movements that carried no timestamp.
        summary["undated_excluded"] = skipped_undated
    return summary


def merge_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any]:
    """Fold several per-Darwin summaries (same period) into one owner-level
    total. Periods are assumed consistent; the first non-empty period is
    reported. ``darwin_count`` records how many summaries were combined."""
    total_in = 0.0
    total_out = 0.0
    counted = 0
    undated = 0
    period: dict[str, Any] | None = None
    by_category: dict[str, dict[str, Any]] = {}

    for s in summaries or []:
        if period is None:
            period = s.get("period")
        total_in += float(s.get("total_in_usdc", 0.0))
        total_out += float(s.get("total_out_usdc", 0.0))
        counted += int(s.get("movement_count", 0))
        undated += int(s.get("undated_excluded", 0))
        for cat, b in (s.get("by_category") or {}).items():
            bucket = by_category.setdefault(
                cat,
                {"direction": b.get("direction", ""), "count": 0, "amount_usdc": 0.0},
            )
            bucket["count"] += int(b.get("count", 0))
            bucket["amount_usdc"] = round(
                bucket["amount_usdc"] + float(b.get("amount_usdc", 0.0)), 6
            )
            if bucket["direction"] != b.get("direction"):
                bucket["direction"] = "mixed"

    merged: dict[str, Any] = {
        "currency": "USDC",
        "period": period or {"year": None, "start_utc": None, "end_utc": None},
        "darwin_count": len(summaries or []),
        "movement_count": counted,
        "total_in_usdc": round(total_in, 6),
        "total_out_usdc": round(total_out, 6),
        "net_usdc": round(total_in - total_out, 6),
        "by_category": by_category,
    }
    if undated:
        merged["undated_excluded"] = undated
    return merged
