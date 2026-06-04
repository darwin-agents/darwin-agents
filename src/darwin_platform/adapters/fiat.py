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
- :class:`MoonPayFiatRamp` — MoonPay's real **hosted-widget + webhook**
  flow (not a synchronous server call). ``deposit_quote`` returns a
  signed buy-widget URL the user completes; MoonPay then fires a
  signed webhook that the platform verifies and reconciles into a wallet
  credit. ``withdraw`` returns a signed sell-widget URL the same way.

All adapters fail-closed: any backend error surfaces as
:class:`FiatRampError` so the platform's harvest scheduler / deposit
flow handles refusals consistently.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
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
    # Hosted-widget ramps (MoonPay) return a signed URL the user completes;
    # synchronous ramps (Stripe/Stub) leave this None.
    widget_url: str | None = None

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
    # "completed" for synchronous ramps (Stripe payout); "pending" for a
    # hosted-widget ramp (MoonPay sell), settled later by webhook.
    status: str = "completed"
    widget_url: str | None = None

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
# MoonPayFiatRamp — hosted-widget on/off-ramp + signed webhooks (doc 17)
# ---------------------------------------------------------------------------

class MoonPayWebhookError(FiatRampError):
    """A MoonPay webhook failed signature / freshness verification."""


class MoonPayFiatRamp:
    """MoonPay ramp via its real **hosted-widget + webhook** model.

    Deposit (on-ramp):
      1. ``deposit_quote`` fetches a buy quote for the rate (best-effort)
         and builds a **signed** buy-widget URL — HMAC-SHA256 of the URL
         search string (incl. the leading ``?``) with the secret key,
         base64, appended as ``signature``. ``externalTransactionId``
         carries the platform quote id; ``externalCustomerId`` carries the
         destination Darwin so the webhook can route the credit statelessly.
      2. The user completes the purchase in MoonPay's widget; the bought
         USDC lands at ``omnibus_wallet_address``.
      3. MoonPay POSTs a signed webhook. ``verify_webhook`` checks the
         ``Moonpay-Signature-V2`` header (HMAC-SHA256 of ``"{t}.{body}"``
         with the webhook key, hex, constant-time compare + freshness);
         ``parse_transaction_event`` normalises it. The supervisor credits
         the Darwin on a ``completed`` buy.

    Withdraw (off-ramp): ``withdraw`` builds a signed sell-widget URL and
    returns a ``pending`` receipt; a ``sell_transaction`` webhook settles it.

    ``confirm_deposit`` is a server-side **poll fallback** for a missed
    webhook: it looks the transaction up by ``externalTransactionId`` via
    the transactions API and confirms iff it is ``completed``.
    """

    name = "moonpay"

    _COMPLETED = "completed"
    _FAILED = "failed"

    def __init__(
        self,
        *,
        api_key: str,                    # publishable pk_ — widget + quote
        secret_key: str | None = None,   # sk_ — URL signing + tx-lookup auth
        webhook_key: str | None = None,  # wk_ — webhook signature key
        omnibus_wallet_address: str | None = None,
        http_client: Any = None,
        buy_widget_url: str = "https://buy.moonpay.com",
        sell_widget_url: str = "https://sell.moonpay.com",
        api_base_url: str = "https://api.moonpay.com",
        base_currency_code: str = "usd",
        quote_currency_code: str = "usdc",
        fee_rate: float = 0.045,
        ttl_seconds: float = 300.0,
        webhook_tolerance_seconds: float = 300.0,
        clock=time.time,
    ) -> None:
        if not api_key:
            raise ValueError("MoonPayFiatRamp requires an api_key")
        self._api_key = api_key
        self._secret_key = secret_key
        self._webhook_key = webhook_key
        self._omnibus = omnibus_wallet_address
        self._http = http_client
        self._buy_url = buy_widget_url.rstrip("/")
        self._sell_url = sell_widget_url.rstrip("/")
        self._api = api_base_url.rstrip("/")
        self._base_ccy = base_currency_code
        self._quote_ccy = quote_currency_code
        self._fee_rate = float(fee_rate)
        self._ttl = float(ttl_seconds)
        self._webhook_tolerance = float(webhook_tolerance_seconds)
        self._clock = clock
        self._quotes: dict[str, FiatQuote] = {}

    # -- helpers ----------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        key = self._secret_key or self._api_key
        return {
            "Authorization": f"Api-Key {key}",
            "Content-Type": "application/json",
        }

    @staticmethod
    def _json(resp: Any) -> Any:
        decoder = getattr(resp, "json", None)
        if callable(decoder):
            try:
                return decoder()
            except Exception:
                pass
        body = getattr(resp, "text", "") or "{}"
        return json.loads(body) if isinstance(body, str) else dict(body)

    @staticmethod
    def _fees(d: dict[str, Any]) -> float:
        return (
            float(d.get("feeAmount") or 0.0)
            + float(d.get("extraFeeAmount") or 0.0)
            + float(d.get("networkFeeAmount") or 0.0)
        )

    def _sign_url(self, base: str, params: dict[str, Any]) -> str:
        """Build ``base?<query>&signature=<sig>`` the way MoonPay's SDKs do:
        urlencode the params (encodes the values), HMAC-SHA256 the search
        string INCLUDING the leading ``?`` with the secret key, base64 the
        digest, URL-encode it, append as ``signature``. Without a secret key
        the URL is returned unsigned (sandbox / test)."""
        query = urllib.parse.urlencode(
            {k: v for k, v in params.items() if v is not None}
        )
        url = f"{base}?{query}"
        if not self._secret_key:
            return url
        digest = hmac.new(
            self._secret_key.encode("utf-8"),
            f"?{query}".encode("utf-8"),
            hashlib.sha256,
        ).digest()
        sig = base64.b64encode(digest).decode("ascii")
        return f"{url}&signature={urllib.parse.quote(sig)}"

    # -- deposit (on-ramp) -----------------------------------------------

    def deposit_quote(
        self, *, amount_usd, payment_method, destination_darwin_id=None,
    ) -> FiatQuote:
        if amount_usd <= 0:
            raise FiatRampError("amount_usd must be > 0")
        quote_id = uuid.uuid4().hex
        usdc, fee = self._buy_quote_rate(amount_usd, payment_method)
        params = {
            "apiKey": self._api_key,
            "baseCurrencyCode": self._base_ccy,
            "baseCurrencyAmount": amount_usd,
            "currencyCode": self._quote_ccy,
            "walletAddress": self._omnibus,
            "paymentMethod": payment_method or None,
            "externalTransactionId": quote_id,
            "externalCustomerId": destination_darwin_id or None,
        }
        widget_url = self._sign_url(self._buy_url, params)
        q = FiatQuote(
            quote_id=quote_id, amount_usd=amount_usd, amount_usdc=usdc,
            fee_usd=fee, rate=usdc / amount_usd, provider=self.name,
            ttl_seconds=self._ttl, quoted_at=self._clock(),
            widget_url=widget_url,
        )
        self._quotes[quote_id] = q
        return q

    def _buy_quote_rate(
        self, amount_usd, payment_method,
    ) -> tuple[float, float]:
        """Best-effort live rate from the buy_quote endpoint; falls back to
        the flat ``fee_rate`` estimate when no http client is wired or the
        call fails (the widget shows the binding rate anyway)."""
        fee = amount_usd * self._fee_rate
        if self._http is None:
            return amount_usd - fee, fee
        try:
            resp = self._http.request(
                "GET",
                f"{self._api}/v3/currencies/{self._quote_ccy}/buy_quote",
                params={
                    "apiKey": self._api_key,
                    "baseCurrencyAmount": amount_usd,
                    "baseCurrencyCode": self._base_ccy,
                    "paymentMethod": payment_method,
                },
            )
            if int(getattr(resp, "status_code", 0)) >= 400:
                return amount_usd - fee, fee
            data = self._json(resp)
        except Exception:
            return amount_usd - fee, fee
        usdc = float(data.get("quoteCurrencyAmount", 0.0) or 0.0)
        live_fee = self._fees(data)
        if live_fee > 0:
            fee = live_fee
        if usdc <= 0:
            usdc = amount_usd - fee
        return usdc, fee

    def confirm_deposit(
        self, *, quote_id, destination_darwin_id=None,
    ) -> FiatDepositReceipt:
        """Server-side reconciliation fallback for a MISSED webhook: look
        the transaction up by ``externalTransactionId`` and confirm iff it
        is ``completed``. The primary confirmation path is the verified
        webhook driving ``supervisor.handle_moonpay_webhook``."""
        if self._http is None:
            raise FiatRampError(
                "moonpay confirm_deposit needs an http client; the primary "
                "path is the verified webhook"
            )
        try:
            resp = self._http.request(
                "GET",
                f"{self._api}/v1/transactions",
                params={"externalTransactionId": quote_id},
                headers=self._auth_headers(),
            )
            status_code = int(getattr(resp, "status_code", 0))
        except Exception as exc:
            raise FiatRampError(f"moonpay tx lookup failed: {exc}") from exc
        if status_code >= 400:
            raise FiatRampError(f"moonpay tx lookup HTTP {status_code}")
        data = self._json(resp)
        txns = data if isinstance(data, list) else (data.get("data") or [])
        tx = next(
            (t for t in txns if str(t.get("status")) == self._COMPLETED), None,
        )
        if tx is None:
            raise FiatRampError(
                f"no completed moonpay tx for externalTransactionId={quote_id}"
            )
        q = self._quotes.pop(quote_id, None)
        amount_usdc = float(
            tx.get("quoteCurrencyAmount") or (q.amount_usdc if q else 0.0)
        )
        amount_usd = float(
            tx.get("baseCurrencyAmount") or (q.amount_usd if q else 0.0)
        )
        fee = self._fees(tx) or (q.fee_usd if q else 0.0)
        return FiatDepositReceipt(
            quote_id=quote_id,
            provider_tx_id=str(tx.get("id") or f"mp-{quote_id}"),
            amount_usd=amount_usd, amount_usdc=amount_usdc, fee_usd=fee,
            provider=self.name,
            destination_darwin_id=(
                destination_darwin_id or tx.get("externalCustomerId")
            ),
            confirmed_at=self._clock(),
        )

    # -- webhook ----------------------------------------------------------

    def verify_webhook(
        self, payload: bytes | str, signature_header: str,
    ) -> dict[str, Any]:
        """Verify a ``Moonpay-Signature-V2`` header and return the parsed
        JSON body. Raises :class:`MoonPayWebhookError` on a missing key,
        malformed header, bad signature, or stale timestamp."""
        if not self._webhook_key:
            raise MoonPayWebhookError("webhook key not configured")
        raw = payload.encode("utf-8") if isinstance(payload, str) else payload
        ts, sig = self._parse_sig_header(signature_header)
        signed = f"{ts}.{raw.decode('utf-8')}"
        expected = hmac.new(
            self._webhook_key.encode("utf-8"),
            signed.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, sig):
            raise MoonPayWebhookError("signature mismatch")
        try:
            age = abs(self._clock() - float(ts))
        except (TypeError, ValueError) as exc:
            raise MoonPayWebhookError(f"bad timestamp: {ts!r}") from exc
        if age > self._webhook_tolerance:
            raise MoonPayWebhookError(f"timestamp too old: {age:.0f}s")
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception as exc:
            raise MoonPayWebhookError(f"invalid JSON body: {exc}") from exc

    @staticmethod
    def _parse_sig_header(header: str) -> tuple[str, str]:
        ts = sig = ""
        for part in (header or "").split(","):
            k, _, v = part.strip().partition("=")
            if k == "t":
                ts = v
            elif k == "s":
                sig = v
        if not ts or not sig:
            raise MoonPayWebhookError(
                f"malformed signature header: {header!r}"
            )
        return ts, sig

    def is_completed(self, status: str) -> bool:
        return str(status) == self._COMPLETED

    def parse_transaction_event(
        self, event: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Normalise a verified MoonPay transaction webhook into the fields
        the platform acts on. Returns ``None`` for event types / shapes we
        don't handle. For a buy the base currency is USD and the quote is
        USDC; for a sell it's the reverse — handled here so the supervisor
        always sees ``amount_usd`` / ``amount_usdc`` consistently."""
        etype = str(event.get("type", ""))
        data = event.get("data") or {}
        if not isinstance(data, dict) or not data:
            return None
        is_sell = etype.startswith("sell_transaction")
        is_buy = etype.startswith("transaction")
        if not (is_sell or is_buy):
            return None
        base = float(data.get("baseCurrencyAmount") or 0.0)
        quote = float(data.get("quoteCurrencyAmount") or 0.0)
        return {
            "kind": "sell" if is_sell else "buy",
            "type": etype,
            "provider_tx_id": str(data.get("id") or ""),
            "external_transaction_id": data.get("externalTransactionId") or None,
            "destination_darwin_id": data.get("externalCustomerId") or None,
            "status": str(data.get("status") or ""),
            "amount_usd": quote if is_sell else base,
            "amount_usdc": base if is_sell else quote,
            "fee_usd": self._fees(data),
        }

    # -- withdraw (off-ramp) ---------------------------------------------

    def withdraw(self, *, amount_usdc, destination) -> FiatWithdrawReceipt:
        if amount_usdc <= 0:
            raise FiatRampError("amount_usdc must be > 0")
        if not destination:
            raise FiatRampError("destination is required")
        ext_id = uuid.uuid4().hex
        params = {
            "apiKey": self._api_key,
            "baseCurrencyCode": self._quote_ccy,   # selling USDC …
            "baseCurrencyAmount": amount_usdc,
            "quoteCurrencyCode": self._base_ccy,   # … for USD
            "externalTransactionId": ext_id,
            "externalCustomerId": destination,
        }
        widget_url = self._sign_url(self._sell_url, params)
        fee = amount_usdc * self._fee_rate
        # A sell settles asynchronously (the user completes the widget; a
        # sell_transaction webhook confirms). The receipt is "pending".
        return FiatWithdrawReceipt(
            provider_tx_id=ext_id,
            amount_usdc=amount_usdc, amount_usd=amount_usdc - fee, fee_usd=fee,
            destination=destination, provider=self.name,
            confirmed_at=self._clock(),
            status="pending", widget_url=widget_url,
        )
