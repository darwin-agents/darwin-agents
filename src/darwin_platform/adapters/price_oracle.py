"""Live price feed for marking non-USDC holdings to USDC (docs/engineering/05).

The genome's :class:`darwin_dna.machinery.holdings.Holdings` marks a trading
Darwin's positions to USDC each heartbeat through a :class:`PriceOracle`
(``price_usdc(symbol) -> float | None``). The genome ships only the
deterministic ``StaticPriceOracle``; without a real feed every position falls
back to its cost basis (safe — never wrongly bankrupts — but not *live*: a
position that has appreciated or crashed is still valued at what was paid).

This module is the platform-side real feed. :class:`HttpPriceOracle`:

- **Fails soft.** Any error (feed down, unknown symbol, bad payload) returns
  ``None``, which the holdings valuation turns into the cost-basis fallback. A
  flaky price feed must never crash a heartbeat or kill a Darwin.
- **Caches with a TTL.** ``perceive`` marks holdings every heartbeat (often
  sub-second), so an uncached oracle would hammer the feed. Quotes are cached
  per coin for ``ttl_seconds``; failures are cached too (briefly) so a down
  feed isn't re-hit on every tick.
- **Is provider-agnostic.** The fetch is an injected ``quote_fn`` (tests pass a
  fake); the default is a CoinGecko ``simple/price``-shaped GET. Symbol →
  coin-id mapping is configurable, with a sane default that strips a ``-USD`` /
  ``-USDC`` quote suffix to the base asset.

The oracle is read-only and holds no funds — it only values, never moves money.
"""

from __future__ import annotations

import time
from typing import Any, Callable


_DEFAULT_COINGECKO = "https://api.coingecko.com/api/v3/simple/price"


class HttpPriceOracle:
    """:class:`~darwin_dna.machinery.holdings.PriceOracle` backed by an HTTP
    price feed.

    Args:
        base_url: feed endpoint (default: CoinGecko ``simple/price``).
        http: an injected HTTP client exposing ``get(url, params=...) -> resp``
            (``resp.json()``); default uses ``httpx``. Ignored when ``quote_fn``
            is given.
        quote_fn: test/override seam — ``quote_fn(coin_id) -> float | None``.
        symbol_map: explicit ``SYMBOL -> coin_id`` overrides (e.g.
            ``{"ETH": "ethereum"}``). Symbols are upper-cased for lookup.
        vs_currency: quote currency the feed returns (default ``"usd"``; USDC is
            treated 1:1 with USD for valuation).
        ttl_seconds: how long a quote (or a failure) stays cached.
        clock: monotonic clock seam for TTL (tests inject a fake).
    """

    def __init__(
        self,
        *,
        base_url: str = _DEFAULT_COINGECKO,
        http: Any | None = None,
        quote_fn: Callable[[str], float | None] | None = None,
        symbol_map: dict[str, str] | None = None,
        vs_currency: str = "usd",
        ttl_seconds: float = 60.0,
        timeout: float = 5.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._base_url = base_url
        self._vs = str(vs_currency).lower()
        self._symbol_map = {
            str(k).upper(): str(v) for k, v in (symbol_map or {}).items()
        }
        self._ttl = float(ttl_seconds)
        self._timeout = float(timeout)
        self._clock = clock
        self._http = http
        self._quote = quote_fn or self._default_quote
        # coin_id -> (price_or_None, expires_at)
        self._cache: dict[str, tuple[float | None, float]] = {}

    # -- PriceOracle -------------------------------------------------------
    def price_usdc(self, symbol: str) -> float | None:
        coin_id = self._coin_id(symbol)
        now = self._clock()
        hit = self._cache.get(coin_id)
        if hit is not None and hit[1] > now:
            return hit[0]
        try:
            raw = self._quote(coin_id)
        except Exception:
            raw = None
        price = self._coerce_positive(raw)
        # Cache successes AND failures (so a down feed isn't re-hit per tick).
        self._cache[coin_id] = (price, now + self._ttl)
        return price

    # -- internals ---------------------------------------------------------
    def _coin_id(self, symbol: str) -> str:
        s = str(symbol).upper().strip()
        if s in self._symbol_map:
            return self._symbol_map[s]
        # Strip a quote suffix (ETH-USD / BTC/USDC / SOL_USDT) → base asset.
        for sep in ("-", "/", "_"):
            if sep in s:
                s = s.split(sep, 1)[0]
                break
        return self._symbol_map.get(s, s.lower())

    @staticmethod
    def _coerce_positive(raw: Any) -> float | None:
        if raw is None:
            return None
        try:
            v = float(raw)
        except (TypeError, ValueError):
            return None
        return v if v > 0 else None

    def _default_quote(self, coin_id: str) -> float | None:
        params = {"ids": coin_id, "vs_currencies": self._vs}
        http = self._http
        if http is None:
            import httpx  # local import: optional dep, only on the real path

            resp = httpx.get(self._base_url, params=params, timeout=self._timeout)
            data = resp.json()
        else:
            resp = http.get(self._base_url, params=params)
            getj = getattr(resp, "json", None)
            data = getj() if callable(getj) else resp
        if isinstance(data, dict):
            entry = data.get(coin_id)
            if isinstance(entry, dict):
                return entry.get(self._vs)
        return None


def build_price_oracle(
    *,
    url: str = _DEFAULT_COINGECKO,
    symbol_map: dict[str, str] | None = None,
    vs_currency: str = "usd",
    ttl_seconds: float = 60.0,
) -> HttpPriceOracle:
    """Construct the production HTTP price oracle. Kept thin so the supervisor
    wiring builds it from a CLI flag without importing httpx at module load."""
    return HttpPriceOracle(
        base_url=url, symbol_map=symbol_map, vs_currency=vs_currency,
        ttl_seconds=ttl_seconds,
    )
