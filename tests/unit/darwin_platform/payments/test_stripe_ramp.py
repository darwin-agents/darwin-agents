"""Tests for the stripe ramp service (docs/engineering/22).

Coverage:
- KYC gate refuses quotes for non-APPROVED subjects.
- Quote → webhook flow: idempotent, omnibus credited, audit recorded.
- Webhook replay for the same provider_tx_id is a no-op.
- Webhook without a known quote_id raises.
- Withdraw refused without KYC; success path debits omnibus + calls ramp.
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.fiat import StubFiatRamp
from darwin_platform.security.kyc import KYCService, KYCStatus, StubKYCProvider
from darwin_platform.payments.omnibus_wallet import OmnibusWallet
from darwin_platform.payments.stripe_ramp import StripeRampError, StripeRampService


@pytest.fixture
def _omnibus():
    from tests.unit.darwin_platform.payments.test_omnibus_wallet import _Chain
    return OmnibusWallet(_Chain(balance=10_000.0))


@pytest.fixture
def _ramp():
    return StubFiatRamp(fee_rate=0.01)


@pytest.fixture
def _kyc():
    return KYCService(provider=StubKYCProvider())


def _make_intent_event(payment_intent_id: str, quote_id: str) -> dict:
    return {
        "type": "payment_intent.succeeded",
        "data": {"object": {
            "id": payment_intent_id,
            "metadata": {"quote_id": quote_id},
        }},
    }


def test_quote_refused_without_kyc(_omnibus, _ramp):
    kyc = KYCService(provider=StubKYCProvider(default_status=KYCStatus.UNKNOWN))
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=kyc)
    with pytest.raises(StripeRampError, match="KYC"):
        svc.create_deposit_quote(
            user_subject="bob", amount_usd=100.0,
            payment_method="card",
        )


def test_quote_records_owner_for_webhook(_omnibus, _ramp, _kyc):
    # Pre-approve alice.
    inq = _kyc.start_inquiry(subject="alice")
    _kyc.refresh_status(subject="alice", inquiry_id=inq["inquiry_id"])

    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=_kyc)
    quote = svc.create_deposit_quote(
        user_subject="alice", amount_usd=100.0, payment_method="card",
        destination_darwin_id="d-1",
    )
    assert quote["amount_usdc"] == pytest.approx(99.0)
    # Sanity: the owner is recorded so the webhook can resolve it.
    assert quote["quote_id"] in svc._quote_owner  # noqa: SLF001


def test_webhook_succeeded_credits_omnibus(_omnibus, _ramp, _kyc):
    inq = _kyc.start_inquiry(subject="alice")
    _kyc.refresh_status(subject="alice", inquiry_id=inq["inquiry_id"])
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=_kyc)
    quote = svc.create_deposit_quote(
        user_subject="alice", amount_usd=100.0, payment_method="card",
    )
    event = _make_intent_event("pi_abc123", quote["quote_id"])
    record = svc.handle_webhook(event)
    assert record.status == "succeeded"
    assert _omnibus.user_credit("alice") == pytest.approx(99.0)


def test_webhook_replay_is_idempotent(_omnibus, _ramp, _kyc):
    inq = _kyc.start_inquiry(subject="alice")
    _kyc.refresh_status(subject="alice", inquiry_id=inq["inquiry_id"])
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=_kyc)
    quote = svc.create_deposit_quote(
        user_subject="alice", amount_usd=100.0, payment_method="card",
    )
    event = _make_intent_event("pi_abc", quote["quote_id"])
    a = svc.handle_webhook(event)
    b = svc.handle_webhook(event)
    # Same record returned; credit not doubled.
    assert a.provider_tx_id == b.provider_tx_id
    assert _omnibus.user_credit("alice") == pytest.approx(99.0)


def test_webhook_unknown_quote_raises(_omnibus, _ramp, _kyc):
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=_kyc)
    event = _make_intent_event("pi_xxx", "quote-never-created")
    with pytest.raises(StripeRampError, match="unknown quote"):
        svc.handle_webhook(event)


def test_webhook_wrong_event_type_raises(_omnibus, _ramp, _kyc):
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=_kyc)
    with pytest.raises(StripeRampError, match="unsupported event"):
        svc.handle_webhook({"type": "checkout.session.completed",
                            "data": {"object": {}}})


def test_withdraw_refused_without_kyc(_omnibus, _ramp):
    kyc = KYCService(provider=StubKYCProvider(default_status=KYCStatus.REJECTED))
    inq = kyc.start_inquiry(subject="bob")
    kyc.refresh_status(subject="bob", inquiry_id=inq["inquiry_id"])
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=kyc)
    with pytest.raises(StripeRampError, match="KYC"):
        svc.withdraw(user_subject="bob", amount_usdc=10.0,
                     destination="bank:1")


def test_withdraw_happy_path_debits_omnibus(_omnibus, _ramp, _kyc):
    inq = _kyc.start_inquiry(subject="alice")
    _kyc.refresh_status(subject="alice", inquiry_id=inq["inquiry_id"])
    svc = StripeRampService(ramp=_ramp, omnibus=_omnibus, kyc=_kyc)
    quote = svc.create_deposit_quote(
        user_subject="alice", amount_usd=100.0, payment_method="card",
    )
    svc.handle_webhook(_make_intent_event("pi_1", quote["quote_id"]))
    out = svc.withdraw(
        user_subject="alice", amount_usdc=20.0,
        destination="bank:dest-1",
    )
    assert out["amount_usdc"] == pytest.approx(20.0)
    assert _omnibus.user_credit("alice") == pytest.approx(79.0)
