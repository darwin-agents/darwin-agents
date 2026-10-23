"""Paid orders a Darwin fulfils (x402 storefront).

The storefront adds an order once the buyer's payment has *verified*; the
genome completes or fails it (``dispatch``); the storefront *settles* the
payment only for a completed order, so a failed one costs the buyer nothing.
Bounded: a Darwin holds at most ``max_pending`` open orders, which also bounds
the LLM spend a burst of buyers can trigger.
"""

from __future__ import annotations

from typing import Any


class OrderBookFull(Exception):
    """Too many open orders for this Darwin right now (HTTP 429)."""


class OrderBook:
    def __init__(self, *, max_pending: int = 3) -> None:
        self._max = int(max_pending)
        self._orders: dict[str, dict[str, Any]] = {}

    def add(self, order_id: str, topic: str) -> None:
        if len(self.pending()) >= self._max:
            raise OrderBookFull(f"{self._max} orders already open")
        self._orders[order_id] = {"id": order_id, "topic": topic, "status": "pending"}

    def pending(self) -> list[dict[str, str]]:
        return [
            {"id": o["id"], "topic": o["topic"]}
            for o in self._orders.values() if o["status"] == "pending"
        ]

    def complete(self, order_id: str, output: dict[str, Any]) -> None:
        if order_id in self._orders:
            self._orders[order_id].update(status="done", output=output)

    def fail(self, order_id: str, reason: str) -> None:
        if order_id in self._orders:
            self._orders[order_id].update(status="failed", reason=reason)

    def get(self, order_id: str) -> dict[str, Any] | None:
        return self._orders.get(order_id)

    def forget(self, order_id: str) -> None:
        """Drop a finished order once the storefront is done with it."""
        self._orders.pop(order_id, None)


def record_sale(wallet: Any, *, phase: str, amount_usdc: float, source: str,
                payer: str) -> None:
    """Revenue bookkeeping around an x402 settlement. ``"expect"`` (before
    settling): a chain-backed wallet labels the payment it will see arrive,
    so the sync books it as revenue — not a deposit — however it races.
    ``"book"`` (after a successful settle): a wallet with no chain (the fake)
    is credited directly. Never both, so revenue is never counted twice."""
    chain_backed = bool(getattr(wallet, "expects_chain_payments", False))
    if phase == "expect" and chain_backed:
        wallet.credit(amount_usdc, source, payer=payer)
    elif phase == "book" and not chain_backed:
        wallet.credit(amount_usdc, source)
