"""Tests for fiat ramp adapters (doc 17)."""

from __future__ import annotations

import json
import types

import pytest

from darwin_platform.adapters.fiat import (
    FiatRamp,
    FiatRampError,
    MoonPayFiatRamp,
    StripeFiatRamp,
    StubFiatRamp,
)


# ---------------------------------------------------------------------------
# StubFiatRamp
# ---------------------------------------------------------------------------

def test_stub_protocol_satisfied():
    assert isinstance(StubFiatRamp(), FiatRamp)


def test_stub_quote_applies_fee():
    r = StubFiatRamp(fee_rate=0.01)
    q = r.deposit_quote(amount_usd=100.0, payment_method="card")
    assert q.fee_usd == pytest.approx(1.0)
    assert q.amount_usdc == pytest.approx(99.0)
    assert q.rate == pytest.approx(0.99)


def test_stub_confirm_consumes_quote():
    r = StubFiatRamp()
    q = r.deposit_quote(amount_usd=50.0, payment_method="card")
    receipt = r.confirm_deposit(quote_id=q.quote_id, destination_darwin_id="d-1")
    assert receipt.amount_usdc == q.amount_usdc
    assert receipt.destination_darwin_id == "d-1"
    # Double-confirm refuses
    with pytest.raises(FiatRampError):
        r.confirm_deposit(quote_id=q.quote_id)


def test_stub_quote_expires():
    now = [1000.0]
    r = StubFiatRamp(ttl_seconds=10, clock=lambda: now[0])
    q = r.deposit_quote(amount_usd=20.0, payment_method="card")
    now[0] = 1020.0  # past TTL
    with pytest.raises(FiatRampError, match="expired"):
        r.confirm_deposit(quote_id=q.quote_id)


def test_stub_withdraw_returns_receipt():
    r = StubFiatRamp()
    rec = r.withdraw(amount_usdc=100.0, destination="acct_bank_x")
    assert rec.destination == "acct_bank_x"
    assert rec.amount_usdc == 100.0


def test_stub_rejects_zero_amounts():
    r = StubFiatRamp()
    with pytest.raises(FiatRampError):
        r.deposit_quote(amount_usd=0, payment_method="card")
    with pytest.raises(FiatRampError):
        r.withdraw(amount_usdc=-1, destination="x")


# ---------------------------------------------------------------------------
# StripeFiatRamp
# ---------------------------------------------------------------------------

class _StripeStub:
    """Minimal Stripe SDK shape — PaymentIntent.create / .retrieve /
    Transfer.create."""

    class _IntentNS:
        def __init__(self, ramp): self.ramp = ramp
        def create(self, **kwargs):
            self.ramp._n += 1
            iid = f"pi_{self.ramp._n}"
            obj = {
                "id": iid, "status": "requires_confirmation",
                "amount": kwargs.get("amount"),
                "metadata": kwargs.get("metadata", {}),
            }
            self.ramp.intents[iid] = obj
            return obj
        def retrieve(self, intent_id):
            return self.ramp.intents.get(intent_id)

    class _XferNS:
        def __init__(self, ramp): self.ramp = ramp
        def create(self, **kwargs):
            self.ramp._n += 1
            self.ramp.payouts.append(kwargs)
            return {"id": f"tr_{self.ramp._n}"}

    def __init__(self):
        self.intents = {}
        self.payouts = []
        self._n = 0
        self.PaymentIntent = self._IntentNS(self)
        self.Transfer = self._XferNS(self)


def test_stripe_quote_creates_payment_intent():
    sk = _StripeStub()
    r = StripeFiatRamp(stripe_client=sk)
    q = r.deposit_quote(amount_usd=100.0, payment_method="pm_card")
    assert q.quote_id.startswith("pi_")
    assert q.provider == "stripe"
    # ~3.2% rough rate (2.9% + $0.30 flat)
    assert q.amount_usdc < 100.0


def test_stripe_confirm_requires_succeeded_status():
    sk = _StripeStub()
    r = StripeFiatRamp(stripe_client=sk)
    q = r.deposit_quote(amount_usd=100.0, payment_method="pm_card")
    # Intent is still "requires_confirmation"
    with pytest.raises(FiatRampError, match="not yet paid"):
        r.confirm_deposit(quote_id=q.quote_id)
    # Flip to succeeded
    sk.intents[q.quote_id]["status"] = "succeeded"
    receipt = r.confirm_deposit(quote_id=q.quote_id,
                                destination_darwin_id="d-1")
    assert receipt.amount_usdc == q.amount_usdc


def test_stripe_withdraw_creates_transfer():
    sk = _StripeStub()
    r = StripeFiatRamp(stripe_client=sk)
    rec = r.withdraw(amount_usdc=200.0, destination="acct_connected_xyz")
    assert rec.provider_tx_id.startswith("tr_")
    assert sk.payouts[0]["destination"] == "acct_connected_xyz"


def test_stripe_requires_client():
    with pytest.raises(ValueError):
        StripeFiatRamp(stripe_client=None)


def test_stripe_constructs_with_real_stripe_sdk():
    """With the [stripe] extra installed, the real ``stripe`` module
    can be passed directly as the client — no shimming needed."""
    pytest.importorskip("stripe")
    import stripe as real_stripe
    # Adapter construction succeeds against the real module.
    ramp = StripeFiatRamp(stripe_client=real_stripe)
    assert ramp.name == "stripe"


def test_stripe_real_sdk_namespaces_resolve():
    """Confirm the namespaces the adapter uses (PaymentIntent,
    Transfer) exist on the real Stripe SDK. Catches a SDK rename
    at PR time."""
    pytest.importorskip("stripe")
    import stripe as real_stripe
    assert hasattr(real_stripe, "PaymentIntent")
    assert hasattr(real_stripe.PaymentIntent, "create")
    assert hasattr(real_stripe.PaymentIntent, "retrieve")
    assert hasattr(real_stripe, "Transfer")
    assert hasattr(real_stripe.Transfer, "create")


# ---------------------------------------------------------------------------
# MoonPayFiatRamp
# ---------------------------------------------------------------------------

class _HTTPStub:
    def __init__(self, *, status=200, body="{}"):
        self.status = status
        self.body = body
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        return types.SimpleNamespace(status_code=self.status, text=self.body)


import hashlib as _hashlib
import hmac as _hmac
import urllib.parse as _urlparse

from darwin_platform.adapters.fiat import MoonPayWebhookError


def _moonpay_sig(webhook_key: str, body: str, ts: str) -> str:
    """A valid Moonpay-Signature-V2 header for ``body`` at time ``ts``."""
    s = _hmac.new(
        webhook_key.encode(), f"{ts}.{body}".encode(), _hashlib.sha256
    ).hexdigest()
    return f"t={ts},s={s}"


def _qs(url: str) -> dict:
    return dict(_urlparse.parse_qsl(_urlparse.urlsplit(url).query))


def test_moonpay_protocol_satisfied():
    assert isinstance(MoonPayFiatRamp(api_key="pk_x"), FiatRamp)


def test_moonpay_buy_delivers_straight_to_the_darwins_wallet():
    """Bought USDC lands at the Darwin's own address (its chain watcher books
    it); the omnibus is only the fallback when no address is known."""
    r = MoonPayFiatRamp(api_key="pk_test", secret_key="sk_test",
                        omnibus_wallet_address="0xOMNI")
    q = r.deposit_quote(amount_usd=50.0, payment_method="credit_debit_card",
                        destination_darwin_id="d-1",
                        destination_address="0x" + "a" * 40)
    assert _qs(q.widget_url)["walletAddress"] == "0x" + "a" * 40


def test_moonpay_requires_api_key():
    with pytest.raises(ValueError):
        MoonPayFiatRamp(api_key="")


def test_moonpay_deposit_quote_builds_signed_widget_url():
    r = MoonPayFiatRamp(
        api_key="pk_test", secret_key="sk_test",
        omnibus_wallet_address="0xOMNI",
    )
    q = r.deposit_quote(
        amount_usd=100.0, payment_method="credit_debit_card",
        destination_darwin_id="d-1",
    )
    assert q.provider == "moonpay"
    assert q.widget_url.startswith("https://buy.moonpay.com?")
    params = _qs(q.widget_url)
    assert params["apiKey"] == "pk_test"
    assert params["baseCurrencyCode"] == "usd"
    assert params["baseCurrencyAmount"] == "100.0"
    assert params["currencyCode"] == "usdc_base"  # Base USDC, not Ethereum L1
    assert params["walletAddress"] == "0xOMNI"
    # Routing fields the webhook reads back:
    assert params["externalTransactionId"] == q.quote_id
    assert params["externalCustomerId"] == "d-1"
    # Signed.
    assert "signature" in params and params["signature"]


def test_moonpay_widget_url_unsigned_without_secret_key():
    r = MoonPayFiatRamp(api_key="pk_test")  # no secret_key
    q = r.deposit_quote(amount_usd=50.0, payment_method="card")
    assert "signature" not in _qs(q.widget_url)


def test_moonpay_quote_rate_from_buy_quote_endpoint():
    body = json.dumps({"quoteCurrencyAmount": 95.0, "feeAmount": 4.0,
                       "networkFeeAmount": 1.0})
    http = _HTTPStub(status=200, body=body)
    r = MoonPayFiatRamp(api_key="pk", http_client=http)
    q = r.deposit_quote(amount_usd=100.0, payment_method="card")
    assert q.amount_usdc == 95.0
    assert q.fee_usd == 5.0  # feeAmount + networkFeeAmount
    # Hit the real v3 buy_quote endpoint with camelCase params.
    call = http.calls[0]
    assert call["url"].endswith("/v3/currencies/usdc_base/buy_quote")
    assert call["kwargs"]["params"]["baseCurrencyAmount"] == 100.0


def test_moonpay_quote_falls_back_to_estimate_when_endpoint_fails():
    http = _HTTPStub(status=500, body="oops")
    r = MoonPayFiatRamp(api_key="pk", http_client=http, fee_rate=0.045)
    q = r.deposit_quote(amount_usd=100.0, payment_method="card")
    # No raise — the widget shows the binding rate; we estimate the display.
    assert q.fee_usd == pytest.approx(4.5)
    assert q.amount_usdc == pytest.approx(95.5)


def test_moonpay_verify_webhook_accepts_valid_signature():
    clock = lambda: 1000.0  # noqa: E731
    r = MoonPayFiatRamp(api_key="pk", webhook_key="wk_secret", clock=clock)
    body = json.dumps({"type": "transaction_updated",
                       "data": {"id": "tx-1", "status": "completed"}})
    header = _moonpay_sig("wk_secret", body, "1000")
    event = r.verify_webhook(body, header)
    assert event["data"]["id"] == "tx-1"


def test_moonpay_verify_webhook_rejects_bad_signature():
    r = MoonPayFiatRamp(api_key="pk", webhook_key="wk_secret",
                        clock=lambda: 1000.0)
    body = json.dumps({"type": "transaction_updated", "data": {}})
    with pytest.raises(MoonPayWebhookError, match="mismatch"):
        r.verify_webhook(body, "t=1000,s=deadbeef")


def test_moonpay_verify_webhook_rejects_stale_timestamp():
    r = MoonPayFiatRamp(api_key="pk", webhook_key="wk_secret",
                        webhook_tolerance_seconds=300, clock=lambda: 100000.0)
    body = json.dumps({"data": {}})
    header = _moonpay_sig("wk_secret", body, "1000")  # far in the past
    with pytest.raises(MoonPayWebhookError, match="too old"):
        r.verify_webhook(body, header)


def test_moonpay_verify_webhook_requires_key():
    r = MoonPayFiatRamp(api_key="pk")  # no webhook key
    with pytest.raises(MoonPayWebhookError, match="not configured"):
        r.verify_webhook("{}", "t=1,s=x")


def test_moonpay_parse_transaction_event_buy_maps_base_to_usd():
    r = MoonPayFiatRamp(api_key="pk")
    parsed = r.parse_transaction_event({
        "type": "transaction_updated",
        "data": {
            "id": "tx-9", "status": "completed",
            "externalTransactionId": "q-9", "externalCustomerId": "d-7",
            "baseCurrencyAmount": 100.0, "quoteCurrencyAmount": 95.0,
            "feeAmount": 5.0,
        },
    })
    assert parsed["kind"] == "buy"
    assert parsed["amount_usd"] == 100.0     # base = USD on a buy
    assert parsed["amount_usdc"] == 95.0     # quote = USDC on a buy
    assert parsed["destination_darwin_id"] == "d-7"
    assert parsed["external_transaction_id"] == "q-9"


def test_moonpay_parse_transaction_event_sell_reverses_currencies():
    r = MoonPayFiatRamp(api_key="pk")
    parsed = r.parse_transaction_event({
        "type": "sell_transaction_updated",
        "data": {"id": "s-1", "status": "completed",
                 "baseCurrencyAmount": 50.0, "quoteCurrencyAmount": 48.0},
    })
    assert parsed["kind"] == "sell"
    assert parsed["amount_usdc"] == 50.0   # base = USDC on a sell
    assert parsed["amount_usd"] == 48.0    # quote = USD on a sell


def test_moonpay_parse_ignores_unknown_event():
    r = MoonPayFiatRamp(api_key="pk")
    assert r.parse_transaction_event({"type": "kyc_updated", "data": {}}) is None
    assert r.parse_transaction_event({"type": "transaction_updated"}) is None


def test_moonpay_confirm_deposit_poll_fallback():
    body = json.dumps([
        {"id": "tx-3", "status": "pending"},
        {"id": "tx-3", "status": "completed", "quoteCurrencyAmount": 90.0,
         "baseCurrencyAmount": 95.0, "feeAmount": 5.0,
         "externalCustomerId": "d-2"},
    ])
    http = _HTTPStub(status=200, body=body)
    r = MoonPayFiatRamp(api_key="pk", secret_key="sk", http_client=http)
    receipt = r.confirm_deposit(quote_id="q-3")
    assert receipt.amount_usdc == 90.0
    assert receipt.destination_darwin_id == "d-2"
    assert http.calls[0]["url"].endswith("/v1/transactions")
    assert http.calls[0]["kwargs"]["params"]["externalTransactionId"] == "q-3"


def test_moonpay_confirm_deposit_raises_when_not_completed():
    http = _HTTPStub(status=200, body=json.dumps([{"id": "t", "status": "pending"}]))
    r = MoonPayFiatRamp(api_key="pk", secret_key="sk", http_client=http)
    with pytest.raises(FiatRampError, match="no completed"):
        r.confirm_deposit(quote_id="q-x")


def test_moonpay_confirm_deposit_requires_http_client():
    r = MoonPayFiatRamp(api_key="pk")
    with pytest.raises(FiatRampError, match="needs an http client"):
        r.confirm_deposit(quote_id="q-x")


def test_moonpay_withdraw_builds_signed_sell_widget_url():
    r = MoonPayFiatRamp(api_key="pk", secret_key="sk", fee_rate=0.04)
    rec = r.withdraw(amount_usdc=100.0, destination="bank-x")
    assert rec.status == "pending"
    assert rec.provider == "moonpay"
    assert rec.fee_usd == pytest.approx(4.0)
    assert rec.widget_url.startswith("https://sell.moonpay.com?")
    params = _qs(rec.widget_url)
    assert params["baseCurrencyCode"] == "usdc_base"   # selling Base USDC
    assert params["quoteCurrencyCode"] == "usd"
    assert params["externalCustomerId"] == "bank-x"
    assert "signature" in params


def test_stripe_quote_persists_and_confirms_after_restart(tmp_path):
    """A quote survives a process restart so a webhook confirm that lands
    later still resolves it; the confirmed quote is then gone (replay-safe)."""
    from darwin_platform.runtime.persistence import PersistenceStore

    db = str(tmp_path / "q.db")
    sk = _StripeStub()
    r1 = StripeFiatRamp(stripe_client=sk)
    r1.attach_store(PersistenceStore(db).fiat_quote_store())
    q = r1.deposit_quote(
        amount_usd=100.0, payment_method="pm_card", destination_darwin_id="d1",
    )
    # Darwin id stamped into the PaymentIntent metadata for the webhook.
    assert sk.intents[q.quote_id]["metadata"]["destination_darwin_id"] == "d1"
    sk.intents[q.quote_id]["status"] = "succeeded"

    # Fresh ramp on the same DB (simulated restart) confirms the old quote.
    r2 = StripeFiatRamp(stripe_client=sk)
    r2.attach_store(PersistenceStore(db).fiat_quote_store())
    receipt = r2.confirm_deposit(quote_id=q.quote_id, destination_darwin_id="d1")
    assert receipt.amount_usdc == q.amount_usdc

    # Quote consumed → a replay (even on another fresh ramp) is rejected.
    r3 = StripeFiatRamp(stripe_client=sk)
    r3.attach_store(PersistenceStore(db).fiat_quote_store())
    with pytest.raises(FiatRampError, match="no local quote"):
        r3.confirm_deposit(quote_id=q.quote_id)
