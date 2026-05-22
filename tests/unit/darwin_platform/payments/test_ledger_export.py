"""Ledger CSV export (docs/engineering/05, 17)."""

from __future__ import annotations

import csv
import io

from darwin_platform.payments.ledger_export import movements_to_csv


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
