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

    def __init__(self):
        self.intents = {}
        self.payouts = []
        self._n = 0

    class _IntentNS:
        def __init__(self, ramp): self.ramp = ramp
        def create(self, **kwargs):
            self.ramp._n += 1
            iid = f"pi_{self.ramp._n}"
            obj = {
                "id": iid, "status": "requires_confirmation",
                "amount": kwargs.get("amount"),
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


def test_moonpay_quote_parses_response():
    body = json.dumps({"id": "q-1", "quote_currency_amount": 95.0,
                       "fee_amount": 5.0})
    http = _HTTPStub(status=200, body=body)
    r = MoonPayFiatRamp(http_client=http, api_key="key")
    q = r.deposit_quote(amount_usd=100.0, payment_method="card")
    assert q.quote_id == "q-1"
    assert q.amount_usdc == 95.0
    assert q.fee_usd == 5.0


def test_moonpay_quote_http_failure_raises():
    http = _HTTPStub(status=500, body="oops")
    r = MoonPayFiatRamp(http_client=http, api_key="k")
    with pytest.raises(FiatRampError, match="HTTP 500"):
        r.deposit_quote(amount_usd=100, payment_method="card")


def test_moonpay_requires_api_key():
    with pytest.raises(ValueError):
        MoonPayFiatRamp(http_client=_HTTPStub(), api_key="")


def test_moonpay_withdraw_posts_sell_transaction():
    http = _HTTPStub(status=200, body=json.dumps({"id": "wd-1",
                                                   "fee_amount": 4.0}))
    r = MoonPayFiatRamp(http_client=http, api_key="k")
    rec = r.withdraw(amount_usdc=100, destination="bank-x")
    assert rec.provider_tx_id == "wd-1"
    assert rec.fee_usd == 4.0
    assert http.calls[0]["url"].endswith("/v3/sell_transactions")
