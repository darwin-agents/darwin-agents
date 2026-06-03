"""Live HTTP price oracle for marking holdings to USDC (doc 05 §Treasury).

Tests inject a fake ``quote_fn`` (or a fake HTTP client) so no network is
touched. The load-bearing properties: it conforms to the genome's PriceOracle
protocol, fails soft to ``None`` (→ cost-basis fallback), and caches with a TTL
so a per-heartbeat mark doesn't hammer the feed.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.holdings import Holdings, PriceOracle
from darwin_platform.adapters.price_oracle import (
    HttpPriceOracle,
    build_price_oracle,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_conforms_to_price_oracle_protocol():
    assert isinstance(HttpPriceOracle(quote_fn=lambda _id: 1.0), PriceOracle)


def test_symbol_is_mapped_then_quoted():
    seen = []

    def quote(coin_id):
        seen.append(coin_id)
        return 3000.0

    o = HttpPriceOracle(quote_fn=quote, symbol_map={"ETH": "ethereum"})
    assert o.price_usdc("ETH") == 3000.0
    assert seen == ["ethereum"]


def test_quote_suffix_is_stripped_to_base_asset():
    seen = []
    o = HttpPriceOracle(
        quote_fn=lambda cid: (seen.append(cid) or 50.0),
        symbol_map={"SOL": "solana"},
    )
    # "SOL-USD" and "SOL/USDC" both resolve to the SOL base asset → "solana".
    assert o.price_usdc("SOL-USD") == 50.0
    assert o.price_usdc("SOL/USDC") == 50.0
    # The second resolved to the same coin_id, so it was served from cache.
    assert seen == ["solana"]


def test_caches_within_ttl_then_refetches():
    calls = {"n": 0}

    def quote(_id):
        calls["n"] += 1
        return 100.0 + calls["n"]

    clock = _Clock()
    o = HttpPriceOracle(quote_fn=quote, ttl_seconds=60.0, clock=clock)
    first = o.price_usdc("BTC")
    second = o.price_usdc("BTC")  # within TTL → cached, no new call
    assert first == second
    assert calls["n"] == 1
    clock.t += 61.0  # past TTL
    third = o.price_usdc("BTC")
    assert calls["n"] == 2
    assert third != first


def test_fails_soft_to_none_on_quote_error():
    def boom(_id):
        raise RuntimeError("feed down")

    o = HttpPriceOracle(quote_fn=boom)
    assert o.price_usdc("ETH") is None


def test_none_and_nonpositive_become_none():
    assert HttpPriceOracle(quote_fn=lambda _id: None).price_usdc("ETH") is None
    assert HttpPriceOracle(quote_fn=lambda _id: 0.0).price_usdc("ETH") is None
    assert HttpPriceOracle(quote_fn=lambda _id: -5).price_usdc("ETH") is None
    assert HttpPriceOracle(quote_fn=lambda _id: "x").price_usdc("ETH") is None


def test_failure_is_cached_so_a_down_feed_isnt_hammered():
    calls = {"n": 0}

    def flaky(_id):
        calls["n"] += 1
        return None

    clock = _Clock()
    o = HttpPriceOracle(quote_fn=flaky, ttl_seconds=30.0, clock=clock)
    assert o.price_usdc("ETH") is None
    assert o.price_usdc("ETH") is None  # cached None within TTL
    assert calls["n"] == 1


# -- default HTTP fetch (CoinGecko-shaped), via a fake client -------------
class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHttp:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def get(self, url, params=None):
        self.calls.append({"url": url, "params": params})
        return _FakeResp(self.payload)


def test_default_quote_parses_coingecko_shape():
    http = _FakeHttp({"ethereum": {"usd": 3210.5}})
    o = HttpPriceOracle(http=http, symbol_map={"ETH": "ethereum"})
    assert o.price_usdc("ETH-USD") == 3210.5
    assert http.calls[0]["params"] == {"ids": "ethereum", "vs_currencies": "usd"}


def test_default_quote_missing_entry_is_none():
    http = _FakeHttp({"ethereum": {"usd": 3000.0}})
    o = HttpPriceOracle(http=http)  # no symbol_map → "doge" id lookups miss
    assert o.price_usdc("DOGE") is None


# -- end-to-end with the genome's Holdings valuation ----------------------
def test_holdings_mark_to_market_uses_the_oracle():
    h = Holdings()
    h.record_buy("ETH-USD", 2.0, 4000.0)  # paid $2000/unit
    oracle = HttpPriceOracle(
        quote_fn=lambda _id: 3000.0, symbol_map={"ETH": "ethereum"},
    )
    mtm = h.mark_to_usdc(oracle)
    assert mtm.value_usdc == pytest.approx(6000.0)  # live 2*3000, not cost basis
    assert mtm.fallback_symbols == []


def test_builder_returns_oracle():
    assert isinstance(build_price_oracle(url="http://feed"), HttpPriceOracle)
