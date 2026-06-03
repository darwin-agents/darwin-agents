"""Non-USDC holdings + mark-to-USDC valuation (doc 05 §Treasury).

The load-bearing property: a position with no live price is valued at its cost
basis, NEVER zero — so an unpriceable holding cannot cause the wrongful
bankruptcy this machinery exists to prevent.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.holdings import (
    Holdings,
    StaticPriceOracle,
    net_worth_usdc,
)


def test_record_buy_accumulates_quantity_and_cost_basis():
    h = Holdings()
    h.record_buy("ETH", 1.0, 3000.0)
    h.record_buy("ETH", 0.5, 1600.0)
    assert h.quantity("ETH") == pytest.approx(1.5)
    pos = h.positions()[0]
    assert pos.cost_basis_usdc == pytest.approx(4600.0)


def test_record_sell_releases_cost_basis_pro_rata():
    h = Holdings()
    h.record_buy("ETH", 2.0, 6000.0)  # $3000/unit cost basis
    released = h.record_sell("ETH", 1.0)
    assert released == pytest.approx(3000.0)
    assert h.quantity("ETH") == pytest.approx(1.0)
    assert h.positions()[0].cost_basis_usdc == pytest.approx(3000.0)


def test_full_sell_removes_position():
    h = Holdings()
    h.record_buy("SOL", 10.0, 1500.0)
    h.record_sell("SOL", 10.0)
    assert h.is_empty
    assert h.quantity("SOL") == 0.0


def test_oversell_clamps_no_shorting():
    h = Holdings()
    h.record_buy("SOL", 5.0, 750.0)
    released = h.record_sell("SOL", 12.0)
    assert released == pytest.approx(750.0)  # only what was held
    assert h.is_empty


def test_mark_to_usdc_uses_live_price():
    h = Holdings()
    h.record_buy("ETH", 2.0, 4000.0)  # paid $2000/unit
    oracle = StaticPriceOracle({"ETH": 3500.0})
    mtm = h.mark_to_usdc(oracle)
    assert mtm.value_usdc == pytest.approx(7000.0)  # 2 * 3500, not cost basis
    assert mtm.marked["ETH"] == pytest.approx(7000.0)
    assert mtm.fallback_symbols == []


def test_mark_to_usdc_falls_back_to_cost_basis_not_zero():
    h = Holdings()
    h.record_buy("OBSCURE", 100.0, 5000.0)
    # Oracle has no quote for OBSCURE -> cost basis, and it's flagged.
    mtm = h.mark_to_usdc(StaticPriceOracle({"ETH": 3000.0}))
    assert mtm.value_usdc == pytest.approx(5000.0)
    assert mtm.fallback_symbols == ["OBSCURE"]


def test_mark_to_usdc_without_oracle_is_cost_basis():
    h = Holdings()
    h.record_buy("ETH", 1.0, 3000.0)
    mtm = h.mark_to_usdc(None)
    assert mtm.value_usdc == pytest.approx(3000.0)
    assert mtm.fallback_symbols == ["ETH"]


def test_oracle_exception_falls_back_to_cost_basis():
    class _Boom:
        def price_usdc(self, symbol):
            raise RuntimeError("feed down")

    h = Holdings()
    h.record_buy("ETH", 1.0, 3000.0)
    mtm = h.mark_to_usdc(_Boom())
    assert mtm.value_usdc == pytest.approx(3000.0)
    assert mtm.fallback_symbols == ["ETH"]


def test_net_worth_adds_cash_and_marked_holdings():
    h = Holdings()
    h.record_buy("ETH", 1.0, 2000.0)
    oracle = StaticPriceOracle({"ETH": 3000.0})
    assert net_worth_usdc(50.0, h, oracle) == pytest.approx(3050.0)


def test_net_worth_without_holdings_is_exactly_cash():
    # Parity: no holdings -> net worth == cash, so existing cash-only behaviour
    # is unchanged.
    assert net_worth_usdc(123.45, None, None) == pytest.approx(123.45)
    assert net_worth_usdc(123.45, Holdings(), None) == pytest.approx(123.45)


def test_buy_rejects_non_positive_quantity():
    h = Holdings()
    with pytest.raises(ValueError):
        h.record_buy("ETH", 0.0, 10.0)
