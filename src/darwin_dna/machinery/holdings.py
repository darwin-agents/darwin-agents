"""Non-USDC holdings + mark-to-USDC valuation (docs/engineering/05 §Treasury).

The Darwin's wallet is authoritative for **USDC cash**, and the double-entry
:class:`~darwin_dna.machinery.ledger.Ledger` reconciles against it every
heartbeat. But a ``trading.operator`` Darwin can acquire **non-USDC positions**
(tokens, an exchange-custodied base asset). Those are not USDC cash, so the
wallet balance does not reflect them — and the bankruptcy check
(:mod:`darwin_dna.orchestrator_skeleton.nodes.check_death`) historically looked
only at cash. The docs-vs-impl audit flagged the consequence: *"a trading
Darwin could be wrongly bankrupted while holding inventory"* — its cash dips
below the minimum-viable floor while it is sitting on a valuable position, and
the floor kills it even though its net worth is healthy.

This module is the genome-side fix: a small, deterministic holdings tracker and
a price-oracle seam so positions can be **marked to USDC** at reconciliation and
folded into the solvency figure.

Design choices (parity + safety):

- **Cash stays authoritative + separate.** This does NOT touch the USDC ledger
  or its on-chain reconciliation — that invariant is unchanged. Holdings are a
  *parallel* asset book; net worth = cash + marked holdings.
- **Cost-basis fallback, never zero.** If the oracle cannot price a symbol (no
  feed, stale, unknown), the position is valued at its **cost basis** (what the
  Darwin paid), not at 0. Valuing an unpriceable asset at 0 would re-introduce
  the exact wrongful-bankruptcy the fix exists to prevent. The fallback symbols
  are surfaced so the caller can audit/alert on them.
- **No shorting at Layer 0.** Quantities floor at 0; an oversell is clamped, not
  allowed to go negative.

The price *source* (a real CoinGecko / DEX / Chainlink feed) is a platform
concern injected through :class:`PriceOracle`; the genome ships only the
deterministic :class:`StaticPriceOracle` for tests + simple deployments.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# Quantities below this are treated as a fully-closed position (float dust).
_DUST = 1e-12


@runtime_checkable
class PriceOracle(Protocol):
    """Marks one symbol to USDC. Returns ``None`` when no quote is available —
    the caller then falls back to cost basis rather than zero."""

    def price_usdc(self, symbol: str) -> float | None: ...


class StaticPriceOracle:
    """Deterministic dict-backed oracle (tests + simple deployments). A symbol
    with no entry returns ``None`` so the cost-basis fallback engages."""

    def __init__(self, prices: dict[str, float] | None = None) -> None:
        self._prices: dict[str, float] = {
            str(k): float(v) for k, v in (prices or {}).items()
        }

    def set(self, symbol: str, price_usdc: float) -> None:
        self._prices[str(symbol)] = float(price_usdc)

    def price_usdc(self, symbol: str) -> float | None:
        return self._prices.get(str(symbol))


@dataclass
class Position:
    """One non-USDC holding. ``cost_basis_usdc`` is the total USDC paid for the
    *current* ``quantity`` (reduced pro-rata on partial sells)."""

    symbol: str
    quantity: float
    cost_basis_usdc: float


@dataclass
class MarkToMarket:
    """Result of valuing the whole book in USDC."""

    value_usdc: float
    # symbol -> the USDC value it was marked at (live price * qty, or cost basis)
    marked: dict[str, float] = field(default_factory=dict)
    # symbols that fell back to cost basis because the oracle had no quote
    fallback_symbols: list[str] = field(default_factory=list)


class Holdings:
    """A Darwin's non-USDC positions. Rebuilt fresh on a sandbox-replacement
    resume like the ledger + memory — the durable record of what was bought is
    the audit chain, not this in-life cache (a production deployment that wants
    cross-resume holdings injects a persistent substrate with the same surface
    via ``platform.holdings``)."""

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}

    # -- mutation ----------------------------------------------------------
    def record_buy(self, symbol: str, quantity: float, cost_usdc: float) -> None:
        """Add ``quantity`` of ``symbol`` acquired for ``cost_usdc`` total."""
        symbol = str(symbol)
        qty = float(quantity)
        cost = float(cost_usdc)
        if qty <= 0:
            raise ValueError("buy quantity must be positive")
        if cost < 0:
            raise ValueError("buy cost must be non-negative")
        pos = self._positions.get(symbol)
        if pos is None:
            self._positions[symbol] = Position(
                symbol=symbol, quantity=qty, cost_basis_usdc=cost,
            )
        else:
            pos.quantity += qty
            pos.cost_basis_usdc += cost

    def record_sell(self, symbol: str, quantity: float) -> float:
        """Reduce ``symbol`` by ``quantity``, releasing cost basis pro-rata.
        Returns the cost basis released (the book value that left the holdings).
        Oversell is clamped to the held quantity (no shorting)."""
        symbol = str(symbol)
        qty = float(quantity)
        if qty <= 0:
            raise ValueError("sell quantity must be positive")
        pos = self._positions.get(symbol)
        if pos is None or pos.quantity <= 0:
            return 0.0
        sold = min(qty, pos.quantity)
        fraction = sold / pos.quantity if pos.quantity > 0 else 1.0
        released = pos.cost_basis_usdc * fraction
        pos.quantity -= sold
        pos.cost_basis_usdc -= released
        if pos.quantity <= _DUST:
            del self._positions[symbol]
        return released

    # -- reads -------------------------------------------------------------
    def quantity(self, symbol: str) -> float:
        pos = self._positions.get(str(symbol))
        return pos.quantity if pos is not None else 0.0

    def positions(self) -> list[Position]:
        return [
            Position(p.symbol, p.quantity, p.cost_basis_usdc)
            for p in self._positions.values()
        ]

    @property
    def is_empty(self) -> bool:
        return not self._positions

    # -- valuation ---------------------------------------------------------
    def mark_to_usdc(self, oracle: PriceOracle | None = None) -> MarkToMarket:
        """Value every open position in USDC. A symbol the oracle cannot price
        (or when no oracle is given) is valued at its cost basis and recorded in
        ``fallback_symbols`` so the caller can surface the data gap."""
        total = 0.0
        marked: dict[str, float] = {}
        fallback: list[str] = []
        for pos in self._positions.values():
            if pos.quantity <= 0:
                continue
            price = None
            if oracle is not None:
                try:
                    price = oracle.price_usdc(pos.symbol)
                except Exception:
                    price = None
            if price is None:
                value = pos.cost_basis_usdc  # conservative: never zero
                fallback.append(pos.symbol)
            else:
                value = pos.quantity * float(price)
            marked[pos.symbol] = value
            total += value
        return MarkToMarket(
            value_usdc=total, marked=marked, fallback_symbols=fallback,
        )


def net_worth_usdc(
    cash_usdc: float,
    holdings: Holdings | None = None,
    oracle: PriceOracle | None = None,
) -> float:
    """Total USDC net worth = authoritative cash + marked non-USDC holdings.
    With no holdings this is exactly the cash balance (parity with the
    cash-only world)."""
    cash = float(cash_usdc)
    if holdings is None or holdings.is_empty:
        return cash
    return cash + holdings.mark_to_usdc(oracle).value_usdc
