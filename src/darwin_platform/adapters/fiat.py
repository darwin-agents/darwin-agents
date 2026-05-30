"""Fiat on/off-ramp adapters (docs/engineering/17 §"Fiat ramps").

Doc 17 ramp model:
- **Deposit**: card / ACH → omnibus USDC, then platform credits the
  Darwin's wallet at the ramp's posted rate (minus fee).
- **Withdraw**: Darwin's USDC harvest → owner's bank, via Stripe
  Connect Express (US) or MoonPay (international fallback).

This module ships the platform-side abstractions; an injected SDK
client (Stripe Python SDK, MoonPay's HTTP API) does the actual work.
The genome never touches fiat — these adapters live on the platform.

Protocols:

- :class:`FiatRamp` — `deposit_quote(amount_usd, payment_method) ->
  Quote`, `confirm_deposit(quote_id) -> DepositReceipt`,
  `withdraw(amount_usdc, destination) -> WithdrawReceipt`.

Stubs you can drop in for tests are :class:`StubFiatRamp` (fakes
acceptance + a fixed rate). Real impls:

- :class:`StripeFiatRamp` — Stripe Payment Intents for card/ACH deposit
  + Stripe Connect Express for bank withdrawal. Requires the
  user-injected Stripe SDK client.
- :class:`MoonPayFiatRamp` — MoonPay's REST API behind an injected
  httpx-shaped client.

All adapters fail-closed: any backend error surfaces as
:class:`FiatRampError` so the platform's harvest scheduler / deposit
flow handles refusals consistently.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol, runtime_checkable


class FiatRampError(Exception):
    """Ramp refused the request — quote stale, insufficient balance,
    KYC required, sanctions hit, etc."""


@dataclass(frozen=True)
class FiatQuote:
    """One quote, valid for ``ttl_seconds``. Confirming after that
    refuses with FiatRampError."""

    quote_id: str
    amount_usd: float
    amount_usdc: float       # net of fee
    fee_usd: float
    rate: float              # USDC per USD (≈ 1.0 - fee_rate)
    provider: str
    ttl_seconds: float
    quoted_at: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FiatDepositReceipt:
    """Confirmed deposit. ``destination_darwin_id`` is set when the
    platform credits a specific Darwin's wallet (it's a metadata-only
    field; the actual credit is the platform's responsibility, not the
    ramp's)."""

    quote_id: str
    provider_tx_id: str
    amount_usd: float
    amount_usdc: float
    fee_usd: float
    provider: str
    destination_darwin_id: str | None
    confirmed_at: float

    def to_json(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FiatWithdrawReceipt:
    """Confirmed off-ramp. The on-chain USDC is debited by the
    platform (RealWallet.harvest_to_owner); this receipt records the
    fiat leg."""

    provider_tx_id: str
    amount_usdc: float
    amount_usd: float
    fee_usd: float
    destination: str  # bank account / payment method id
    provider: str
    confirmed_at: float

    def to_json(self) -> dict:
        return asdict(self)


@runtime_checkable
class FiatRamp(Protocol):
    """The platform-facing ramp interface. All three methods may raise
    :class:`FiatRampError` on backend failure."""

    name: str  # provider tag for audit + dashboards

    def deposit_quote(
        self,
        *,
        amount_usd: float,
        payment_method: str,
        destination_darwin_id: str | None = None,
    ) -> FiatQuote: ...

    def confirm_deposit(
        self,
        *,
        quote_id: str,
        destination_darwin_id: str | None = None,
    ) -> FiatDepositReceipt: ...

    def withdraw(
        self,
        *,
        amount_usdc: float,
        destination: str,
    ) -> FiatWithdrawReceipt: ...


# ---------------------------------------------------------------------------
# StubFiatRamp — for tests + development. Deterministic 1% fee.
# ---------------------------------------------------------------------------

class StubFiatRamp:
    """In-memory ramp. Quotes never expire below ``ttl_seconds``; all
    payment methods accepted; sequential receipt ids for assertion."""

    name = "stub"

    def __init__(
        self,
        *,
        fee_rate: float = 0.01,
        ttl_seconds: float = 60.0,
        clock=time.time,
    ) -> None:
        self._fee_rate = float(fee_rate)
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._quotes: dict[str, FiatQuote] = {}
        self._tx_counter = 0

    def deposit_quote(
        self, *, amount_usd, payment_method, destination_darwin_id=None,
    ) -> FiatQuote:
        if amount_usd <= 0:
            raise FiatRampError("amount_usd must be > 0")
        fee = amount_usd * self._fee_rate
        usdc = amount_usd - fee
        q = FiatQuote(
            quote_id=uuid.uuid4().hex,
            amount_usd=amount_usd,
            amount_usdc=usdc,
            fee_usd=fee,
            rate=usdc / amount_usd,
            provider=self.name,
            ttl_seconds=self._ttl,
            quoted_at=self._clock(),
        )
        self._quotes[q.quote_id] = q
        return q

    def confirm_deposit(
        self, *, quote_id, destination_darwin_id=None,
    ) -> FiatDepositReceipt:
        q = self._quotes.pop(quote_id, None)
        if q is None:
            raise FiatRampError(f"unknown or already-consumed quote {quote_id}")
        if self._clock() > q.quoted_at + q.ttl_seconds:
            raise FiatRampError("quote expired")
        self._tx_counter += 1
        return FiatDepositReceipt(
            quote_id=quote_id,
            provider_tx_id=f"stub-dep-{self._tx_counter}",
            amount_usd=q.amount_usd, amount_usdc=q.amount_usdc,
            fee_usd=q.fee_usd, provider=self.name,
            destination_darwin_id=destination_darwin_id,
            confirmed_at=self._clock(),
        )

    def withdraw(self, *, amount_usdc, destination) -> FiatWithdrawReceipt:
        if amount_usdc <= 0:
            raise FiatRampError("amount_usdc must be > 0")
        if not destination:
            raise FiatRampError("destination is required")
        fee = amount_usdc * self._fee_rate
        usd = amount_usdc - fee
        self._tx_counter += 1
        return FiatWithdrawReceipt(
            provider_tx_id=f"stub-wd-{self._tx_counter}",
            amount_usdc=amount_usdc, amount_usd=usd, fee_usd=fee,
            destination=destination, provider=self.name,
            confirmed_at=self._clock(),
        )


# ---------------------------------------------------------------------------
# StripeFiatRamp — Stripe Payment Intents + Stripe Connect Express
# ---------------------------------------------------------------------------

class StripeFiatRamp:
    """Real Stripe ramp. The injected ``stripe_client`` is the Stripe
    Python SDK (``import stripe``); tests inject a stub with the same
    method shape.

    Deposit flow:
      1. ``deposit_quote`` creates a Stripe Payment Intent off-session.
      2. Frontend confirms the intent on the user's card/ACH method.
      3. ``confirm_deposit`` polls the intent's status — when
         ``succeeded``, the platform's USDC reserve is moved into
         the Darwin's wallet (this module returns the receipt; the
         caller does the wallet credit).

    Withdraw flow:
      1. Platform debits the Darwin's wallet on-chain.
      2. ``withdraw`` triggers a Stripe Connect Express payout to the
         destination connected-account id.
    """

    name = "stripe"

    def __init__(
        self,
        *,
        stripe_client: Any,
        fee_rate: float = 0.029,  # Stripe's standard card rate
        flat_fee_usd: float = 0.30,
        ttl_seconds: float = 600.0,
        clock=time.time,
    ) -> None:
        if stripe_client is None:
            raise ValueError("StripeFiatRamp requires a stripe_client")
        self._stripe = stripe_client
        self._fee_rate = float(fee_rate)
        self._flat = float(flat_fee_usd)
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._quotes: dict[str, dict] = {}
        # Optional write-through persistence (set via attach_store); None
        # keeps quotes in-memory only. With a store, a quote confirmed by
        # webhook after a restart still resolves, and a consumed quote
        # makes a replay a safe no-op.
        self._store: Any | None = None

    def attach_store(self, store: Any) -> None:
        """Bind a persistence store and rehydrate outstanding quotes."""
        self._store = store
        self._quotes = {q["quote_id"]: q for q in store.load_all()}

    def deposit_quote(
        self, *, amount_usd, payment_method, destination_darwin_id=None,
    ) -> FiatQuote:
        if amount_usd <= 0:
            raise FiatRampError("amount_usd must be > 0")
        fee = amount_usd * self._fee_rate + self._flat
        if fee >= amount_usd:
            raise FiatRampError("amount too small to cover Stripe fee")
        usdc = amount_usd - fee
        # Create off-session Payment Intent. Stamp the destination Darwin
        # into the intent metadata so the webhook (which only sees the
        # PaymentIntent) can resolve which wallet to credit on success.
        meta = {"darwin_platform_quote": "true"}
        if destination_darwin_id:
            meta["destination_darwin_id"] = str(destination_darwin_id)
        try:
            intent = self._stripe.PaymentIntent.create(
                amount=int(amount_usd * 100),  # USD cents
                currency="usd",
                payment_method=payment_method,
                automatic_payment_methods={"enabled": True},
                metadata=meta,
            )
        except Exception as exc:
            raise FiatRampError(f"stripe quote failed: {exc}") from exc
        intent_id = (
            intent.get("id") if isinstance(intent, dict)
            else getattr(intent, "id", None)
        )
        if not intent_id:
            raise FiatRampError("stripe returned no intent id")
        q = FiatQuote(
            quote_id=intent_id, amount_usd=amount_usd, amount_usdc=usdc,
            fee_usd=fee, rate=usdc / amount_usd, provider=self.name,
            ttl_seconds=self._ttl, quoted_at=self._clock(),
        )
        self._quotes[q.quote_id] = q.to_json()
        if self._store is not None:
            self._store.save(q.to_json())
        return q

    def confirm_deposit(
        self, *, quote_id, destination_darwin_id=None,
    ) -> FiatDepositReceipt:
        try:
            intent = self._stripe.PaymentIntent.retrieve(quote_id)
        except Exception as exc:
            raise FiatRampError(f"stripe retrieve failed: {exc}") from exc
        status = (
            intent.get("status") if isinstance(intent, dict)
            else getattr(intent, "status", "")
        )
        if status != "succeeded":
            raise FiatRampError(
                f"stripe intent {quote_id} status={status!r}; not yet paid"
            )
        q_dict = self._quotes.pop(quote_id, None)
        if q_dict is None:
            raise FiatRampError(f"no local quote for intent {quote_id}")
        # Quote consumed — delete from the store so it can't be replayed.
        if self._store is not None:
            self._store.delete(quote_id)
        return FiatDepositReceipt(
            quote_id=quote_id,
            provider_tx_id=quote_id,
            amount_usd=q_dict["amount_usd"],
            amount_usdc=q_dict["amount_usdc"],
            fee_usd=q_dict["fee_usd"],
            provider=self.name,
            destination_darwin_id=destination_darwin_id,
            confirmed_at=self._clock(),
        )

    def withdraw(self, *, amount_usdc, destination) -> FiatWithdrawReceipt:
        if amount_usdc <= 0:
            raise FiatRampError("amount_usdc must be > 0")
        if not destination:
            raise FiatRampError("destination connected account id is required")
        # Stripe Connect Express transfer.
        try:
            payout = self._stripe.Transfer.create(
                amount=int(amount_usdc * 100),
                currency="usd",
                destination=destination,
                metadata={"darwin_platform_payout": "true"},
            )
        except Exception as exc:
            raise FiatRampError(f"stripe payout failed: {exc}") from exc
        tx_id = (
            payout.get("id") if isinstance(payout, dict)
            else getattr(payout, "id", None)
        )
        if not tx_id:
            raise FiatRampError("stripe returned no payout id")
        fee = amount_usdc * self._fee_rate + self._flat
        return FiatWithdrawReceipt(
            provider_tx_id=tx_id, amount_usdc=amount_usdc,
            amount_usd=amount_usdc - fee, fee_usd=fee,
            destination=destination, provider=self.name,
            confirmed_at=self._clock(),
        )


# ---------------------------------------------------------------------------
# MoonPayFiatRamp — HTTP API, fallback when Stripe isn't available
# ---------------------------------------------------------------------------

class MoonPayFiatRamp:
    """MoonPay ramp via REST. Suitable for international flows where
    Stripe isn't available. The injected ``http_client`` defaults to
    ``httpx.Client`` — production passes one with the API key set as
    a default header."""

    name = "moonpay"

    def __init__(
        self,
        *,
        http_client: Any,
        api_key: str,
        base_url: str = "https://api.moonpay.com",
        fee_rate: float = 0.045,
        ttl_seconds: float = 300.0,
        clock=time.time,
    ) -> None:
        if not api_key:
            raise ValueError("MoonPayFiatRamp requires an api_key")
        self._http = http_client
        self._api_key = api_key
        self._base = base_url.rstrip("/")
        self._fee_rate = float(fee_rate)
        self._ttl = float(ttl_seconds)
        self._clock = clock
        self._quotes: dict[str, FiatQuote] = {}

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Api-Key {self._api_key}",
            "Content-Type": "application/json",
        }

    def deposit_quote(
        self, *, amount_usd, payment_method, destination_darwin_id=None,
    ) -> FiatQuote:
        if amount_usd <= 0:
            raise FiatRampError("amount_usd must be > 0")
        try:
            resp = self._http.request(
                "GET",
                f"{self._base}/v3/currencies/usdc/buy_quote",
                params={"amount": amount_usd, "paymentMethod": payment_method},
                headers=self._headers(),
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            raise FiatRampError(f"moonpay quote failed: {exc}") from exc
        if status >= 400:
            raise FiatRampError(f"moonpay quote returned HTTP {status}")
        import json
        body = getattr(resp, "text", "") or "{}"
        try:
            data = json.loads(body) if isinstance(body, str) else dict(body)
        except Exception as exc:
            raise FiatRampError(f"moonpay returned invalid JSON: {exc}") from exc
        usdc = float(data.get("quote_currency_amount", 0.0))
        fee = float(data.get("fee_amount", amount_usd * self._fee_rate))
        if usdc <= 0:
            usdc = amount_usd - fee
        q = FiatQuote(
            quote_id=str(data.get("id") or uuid.uuid4().hex),
            amount_usd=amount_usd, amount_usdc=usdc, fee_usd=fee,
            rate=usdc / amount_usd, provider=self.name,
            ttl_seconds=self._ttl, quoted_at=self._clock(),
        )
        self._quotes[q.quote_id] = q
        return q

    def confirm_deposit(
        self, *, quote_id, destination_darwin_id=None,
    ) -> FiatDepositReceipt:
        q = self._quotes.get(quote_id)
        if q is None:
            raise FiatRampError(f"unknown moonpay quote {quote_id}")
        if self._clock() > q.quoted_at + q.ttl_seconds:
            raise FiatRampError("moonpay quote expired")
        try:
            resp = self._http.request(
                "POST",
                f"{self._base}/v3/transactions",
                content=__import__("json").dumps({
                    "quote_id": quote_id,
                    "darwin_id": destination_darwin_id,
                }),
                headers=self._headers(),
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            raise FiatRampError(f"moonpay confirm failed: {exc}") from exc
        if status >= 400:
            raise FiatRampError(
                f"moonpay confirm returned HTTP {status}"
            )
        import json
        body = getattr(resp, "text", "") or "{}"
        try:
            data = json.loads(body) if isinstance(body, str) else dict(body)
        except Exception as exc:
            raise FiatRampError(f"moonpay confirm invalid JSON: {exc}") from exc
        self._quotes.pop(quote_id, None)
        return FiatDepositReceipt(
            quote_id=quote_id,
            provider_tx_id=str(data.get("id") or f"mp-{quote_id}"),
            amount_usd=q.amount_usd, amount_usdc=q.amount_usdc,
            fee_usd=q.fee_usd, provider=self.name,
            destination_darwin_id=destination_darwin_id,
            confirmed_at=self._clock(),
        )

    def withdraw(self, *, amount_usdc, destination) -> FiatWithdrawReceipt:
        if amount_usdc <= 0:
            raise FiatRampError("amount_usdc must be > 0")
        if not destination:
            raise FiatRampError("destination is required")
        try:
            resp = self._http.request(
                "POST",
                f"{self._base}/v3/sell_transactions",
                content=__import__("json").dumps({
                    "amount_usdc": amount_usdc,
                    "destination": destination,
                }),
                headers=self._headers(),
            )
            status = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            raise FiatRampError(f"moonpay withdraw failed: {exc}") from exc
        if status >= 400:
            raise FiatRampError(
                f"moonpay withdraw returned HTTP {status}"
            )
        import json
        body = getattr(resp, "text", "") or "{}"
        try:
            data = json.loads(body) if isinstance(body, str) else dict(body)
        except Exception as exc:
            raise FiatRampError(f"moonpay withdraw invalid JSON: {exc}") from exc
        fee = float(data.get("fee_amount", amount_usdc * self._fee_rate))
        return FiatWithdrawReceipt(
            provider_tx_id=str(data.get("id") or f"mp-wd-{uuid.uuid4().hex}"),
            amount_usdc=amount_usdc, amount_usd=amount_usdc - fee,
            fee_usd=fee, destination=destination, provider=self.name,
            confirmed_at=self._clock(),
        )
