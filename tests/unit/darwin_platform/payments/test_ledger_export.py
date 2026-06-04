"""Ledger CSV export (docs/engineering/05, 17)."""

from __future__ import annotations

import csv
import io

from darwin_platform.payments.ledger_export import (
    merge_summaries,
    movements_to_csv,
    movements_to_summary,
)

# 2024-06-01 and 2023-06-01 (UTC) — straddle a tax-year boundary.
_TS_2024 = 1_717_200_000.0
_TS_2023 = 1_685_577_600.0


def _rows(text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(text)))


def test_empty_ledger_yields_a_header_only_file():
    text = movements_to_csv([])
    assert _rows(text) == []
    assert text.splitlines()[0].startswith("seq,timestamp_utc,type")


def test_debit_movement_is_normalised():
    movements = [{"debit": {
        "to": "vendor.example", "amount_usdc": 12.5, "memo": "ad spend",
        "kind": "payment", "ts": 1_700_000_000.0,
    }}]
    r = _rows(movements_to_csv(movements))[0]
    assert r["type"] == "debit"
    assert r["direction"] == "out"
    assert r["amount_usdc"] == "12.5"
    assert r["counterparty"] == "vendor.example"
    assert r["category"] == "payment"
    assert r["memo"] == "ad spend"
    assert r["timestamp_utc"].startswith("2023-11-14")  # UTC ISO-8601


def test_credit_movement_is_inbound():
    movements = [{"credit": {"from": "blog.example/affiliate",
                             "amount_usdc": 30.0, "ts": 1_700_000_000.0}}]
    r = _rows(movements_to_csv(movements))[0]
    assert r["type"] == "credit"
    assert r["direction"] == "in"
    assert r["counterparty"] == "blog.example/affiliate"


def test_harvest_and_reproduction_movements():
    movements = [
        {"harvest": {"destination": "bank://x", "amount_usdc": 50.0,
                     "kind": "owner_net", "ts": 1_700_000_000.0}},
        {"reproduction_transfer": {"amount_usdc": 20.0, "ts": 1_700_000_000.0}},
    ]
    rows = _rows(movements_to_csv(movements))
    assert rows[0]["type"] == "harvest"
    assert rows[0]["counterparty"] == "bank://x"
    assert rows[0]["category"] == "owner_net"
    assert rows[1]["type"] == "reproduction_transfer"
    assert rows[1]["category"] == "reproduction_transfer"  # no kind -> type
    assert rows[1]["counterparty"] == ""


def test_skips_empty_or_malformed_movements():
    movements = [
        {},
        {"debit": None},
        {"credit": {"from": "x", "amount_usdc": 1.0, "ts": 1_700_000_000.0}},
    ]
    rows = _rows(movements_to_csv(movements))
    assert len(rows) == 1  # only the valid credit row survives
    assert rows[0]["type"] == "credit"


def test_seq_is_the_movement_index():
    movements = [
        {"credit": {"from": "a", "amount_usdc": 1.0, "ts": 1_700_000_000.0}},
        {"credit": {"from": "b", "amount_usdc": 2.0, "ts": 1_700_000_000.0}},
    ]
    rows = _rows(movements_to_csv(movements))
    assert [r["seq"] for r in rows] == ["0", "1"]


# -- summary ---------------------------------------------------------------

def _ledger() -> list[dict]:
    return [
        {"credit": {"from": "affiliate", "amount_usdc": 100.0,
                    "kind": "revenue", "ts": _TS_2024}},
        {"credit": {"from": "affiliate", "amount_usdc": 50.0,
                    "kind": "revenue", "ts": _TS_2024}},
        {"debit": {"to": "vendor", "amount_usdc": 30.0,
                   "kind": "ad_spend", "ts": _TS_2024}},
        {"harvest": {"destination": "bank", "amount_usdc": 20.0,
                     "kind": "owner_net", "ts": _TS_2024}},
        # Prior tax year — must be excluded from a 2024 summary.
        {"credit": {"from": "affiliate", "amount_usdc": 999.0,
                    "kind": "revenue", "ts": _TS_2023}},
    ]


def test_lifetime_summary_totals_every_movement():
    s = movements_to_summary(_ledger())
    assert s["currency"] == "USDC"
    assert s["movement_count"] == 5
    assert s["total_in_usdc"] == 1149.0   # 100 + 50 + 999
    assert s["total_out_usdc"] == 50.0    # 30 ad_spend + 20 harvest
    assert s["net_usdc"] == 1099.0
    assert s["period"]["year"] is None


def test_year_summary_filters_to_the_tax_year():
    s = movements_to_summary(_ledger(), year=2024)
    assert s["movement_count"] == 4          # the 2023 credit is dropped
    assert s["total_in_usdc"] == 150.0
    assert s["total_out_usdc"] == 50.0
    assert s["net_usdc"] == 100.0
    assert s["period"]["year"] == 2024
    assert s["period"]["start_utc"].startswith("2024-01-01")
    assert s["period"]["end_utc"].startswith("2025-01-01")


def test_by_category_breakdown_and_mixed_direction():
    s = movements_to_summary(_ledger(), year=2024)
    cats = s["by_category"]
    assert cats["revenue"] == {"direction": "in", "count": 2,
                               "amount_usdc": 150.0}
    assert cats["ad_spend"]["direction"] == "out"
    assert cats["owner_net"]["amount_usdc"] == 20.0


def test_explicit_range_is_half_open():
    movements = _ledger()
    # [TS_2024, TS_2024+1) includes the three movements stamped exactly at
    # TS_2024 boundary-inclusive on start, and the harvest too (same ts).
    s = movements_to_summary(movements, start_ts=_TS_2024, end_ts=_TS_2024 + 1)
    assert s["movement_count"] == 4
    # end is exclusive: a range ending at TS_2024 excludes them.
    s2 = movements_to_summary(movements, start_ts=0, end_ts=_TS_2024)
    assert s2["movement_count"] == 1   # only the 2023 credit


def test_dated_period_excludes_undated_movements_and_reports_count():
    movements = [
        {"credit": {"from": "a", "amount_usdc": 10.0, "kind": "revenue",
                    "ts": _TS_2024}},
        {"credit": {"from": "b", "amount_usdc": 5.0, "kind": "revenue"}},  # no ts
    ]
    s = movements_to_summary(movements, year=2024)
    assert s["movement_count"] == 1
    assert s["total_in_usdc"] == 10.0
    assert s["undated_excluded"] == 1
    # Lifetime (undated period) keeps both — nothing to exclude.
    assert "undated_excluded" not in movements_to_summary(movements)


def test_empty_ledger_summary_is_all_zero():
    s = movements_to_summary([])
    assert s["movement_count"] == 0
    assert s["total_in_usdc"] == 0.0
    assert s["net_usdc"] == 0.0
    assert s["by_category"] == {}


def test_merge_summaries_folds_per_darwin_into_owner_total():
    a = movements_to_summary(_ledger(), year=2024)
    b = movements_to_summary([
        {"credit": {"from": "x", "amount_usdc": 10.0, "kind": "revenue",
                    "ts": _TS_2024}},
        {"debit": {"to": "y", "amount_usdc": 4.0, "kind": "fees",
                   "ts": _TS_2024}},
    ], year=2024)
    m = merge_summaries([a, b])
    assert m["darwin_count"] == 2
    assert m["movement_count"] == 6
    assert m["total_in_usdc"] == 160.0    # 150 + 10
    assert m["total_out_usdc"] == 54.0    # 50 + 4
    assert m["net_usdc"] == 106.0
    assert m["by_category"]["revenue"]["count"] == 3
    assert m["by_category"]["fees"]["amount_usdc"] == 4.0
    assert m["period"]["year"] == 2024


def test_merge_empty_is_zero():
    m = merge_summaries([])
    assert m["darwin_count"] == 0
    assert m["movement_count"] == 0
    assert m["by_category"] == {}
