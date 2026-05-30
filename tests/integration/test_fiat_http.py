"""HTTP routes for fiat ramp (doc 17)."""

from __future__ import annotations

import pytest

from darwin_platform.adapters.fiat import StubFiatRamp


@pytest.mark.asyncio
async def test_fiat_quote_no_ramp_returns_409():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            "/platform/fiat/deposit_quote",
            json={"amount_usd": 100.0, "payment_method": "card"},
        )
        assert r.status_code == 409
        assert "not configured" in r.json()["detail"]
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fiat_quote_with_ramp_returns_quote():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(fiat_ramp=StubFiatRamp())
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            "/platform/fiat/deposit_quote",
            json={"amount_usd": 100.0, "payment_method": "card"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["provider"] == "stub"
        assert body["amount_usdc"] == pytest.approx(99.0)
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fiat_confirm_credits_darwin_wallet():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    ramp = StubFiatRamp()
    sup = Supervisor(fiat_ramp=ramp)
    status = await sup.spawn(opening_usdc=10.0)
    did = status.darwin_id
    pre_balance = sup._darwins[did].stack.wallet.balance_usdc()
    app = build_app(sup)
    with TestClient(app) as client:
        # Quote first.
        q = client.post(
            "/platform/fiat/deposit_quote",
            json={"amount_usd": 50.0, "payment_method": "card"},
        )
        quote_id = q.json()["quote_id"]
        r = client.post(
            "/platform/fiat/confirm_deposit",
            json={"quote_id": quote_id, "destination_darwin_id": did},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["destination_darwin_id"] == did
    post_balance = sup._darwins[did].stack.wallet.balance_usdc()
    # 50 USD * (1 - 0.01) = 49.50 USDC credited
    assert post_balance == pytest.approx(pre_balance + 49.50)
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fiat_confirm_unknown_darwin_returns_404():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    ramp = StubFiatRamp()
    sup = Supervisor(fiat_ramp=ramp)
    app = build_app(sup)
    with TestClient(app) as client:
        q = client.post(
            "/platform/fiat/deposit_quote",
            json={"amount_usd": 50.0, "payment_method": "card"},
        )
        quote_id = q.json()["quote_id"]
        r = client.post(
            "/platform/fiat/confirm_deposit",
            json={
                "quote_id": quote_id,
                "destination_darwin_id": "ghost-id",
            },
        )
        assert r.status_code == 404
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fiat_withdraw_route():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(fiat_ramp=StubFiatRamp())
    app = build_app(sup)
    with TestClient(app) as client:
        r = client.post(
            "/platform/fiat/withdraw",
            json={"amount_usdc": 100.0, "destination": "acct_bank_x"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["destination"] == "acct_bank_x"
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_fiat_routes_audit_logged():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(fiat_ramp=StubFiatRamp())
    app = build_app(sup)
    with TestClient(app) as client:
        client.post(
            "/platform/fiat/deposit_quote",
            json={"amount_usd": 25.0, "payment_method": "ach"},
        )
        client.post(
            "/platform/fiat/withdraw",
            json={"amount_usdc": 10.0, "destination": "bank-x"},
        )
    assert len(sup.audit_log.entries(action="fiat_deposit_quote")) == 1
    assert len(sup.audit_log.entries(action="fiat_withdraw")) == 1
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_stripe_webhook_confirms_then_idempotent_replay():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    stripe = pytest.importorskip("stripe")
    import hashlib
    import hmac
    import json
    import time as _t

    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(fiat_ramp=StubFiatRamp())
    status = await sup.spawn(opening_usdc=10.0, actor="alice@x")
    did = status.darwin_id
    secret = "whsec_test_secret"
    app = build_app(sup, stripe_webhook_secret=secret)

    def _sign(payload: str) -> dict:
        ts = int(_t.time())
        mac = hmac.new(
            secret.encode(), f"{ts}.{payload}".encode(), hashlib.sha256
        ).hexdigest()
        return {"Stripe-Signature": f"t={ts},v1={mac}"}

    with TestClient(app) as client:
        q = client.post(
            "/platform/fiat/deposit_quote",
            json={"amount_usd": 50.0, "payment_method": "card",
                  "destination_darwin_id": did},
        )
        assert q.status_code == 200, q.text
        quote_id = q.json()["quote_id"]
        event = json.dumps({
            "id": "evt_test_1",
            "object": "event",
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": quote_id, "object": "payment_intent",
                                "metadata": {"destination_darwin_id": did}}},
        })

        pre = sup._darwins[did].stack.wallet.balance_usdc()
        r1 = client.post("/webhooks/stripe", content=event, headers=_sign(event))
        assert r1.status_code == 200, r1.text
        assert r1.json()["status"] == "confirmed"
        post = sup._darwins[did].stack.wallet.balance_usdc()
        assert post > pre  # wallet credited

        # Replay → idempotent (quote already consumed) — no double credit.
        r2 = client.post("/webhooks/stripe", content=event, headers=_sign(event))
        assert r2.status_code == 200
        assert r2.json()["status"] == "skipped"
        assert sup._darwins[did].stack.wallet.balance_usdc() == post

        # Forged signature → 400.
        bad = client.post(
            "/webhooks/stripe", content=event,
            headers={"Stripe-Signature": "t=1,v1=deadbeef"},
        )
        assert bad.status_code == 400
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_stripe_webhook_without_secret_returns_503():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor(fiat_ramp=StubFiatRamp())
    app = build_app(sup)  # no stripe_webhook_secret
    with TestClient(app) as client:
        r = client.post(
            "/webhooks/stripe", content="{}",
            headers={"Stripe-Signature": "x"},
        )
        assert r.status_code == 503
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_manual_credit_endpoint_off_by_default():
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from darwin_platform.api import build_app
    from darwin_platform.runtime.supervisor import Supervisor

    sup = Supervisor()
    body = {"user_subject": "u@x", "amount_usdc": 1.0}
    # Default: endpoint disabled → 403 (before any omnibus check).
    with TestClient(build_app(sup)) as client:
        r = client.post("/platform/omnibus/credit", json=body)
        assert r.status_code == 403
        assert "disabled" in r.json()["detail"]
    # Opt-in: gate opens; with no omnibus wired it then 409s (gate passed).
    with TestClient(build_app(sup, allow_manual_credit=True)) as client:
        r = client.post("/platform/omnibus/credit", json=body)
        assert r.status_code == 409
        assert "omnibus" in r.json()["detail"].lower()
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_stripe_webhook_durable_idempotency_across_restart(tmp_path):
    pytest.importorskip("fastapi")
    from darwin_platform.runtime.supervisor import Supervisor

    db = str(tmp_path / "wh.db")
    sup = Supervisor(fiat_ramp=StubFiatRamp(), persistence_path=db)
    st = await sup.spawn(opening_usdc=10.0, actor="alice@x")
    did = st.darwin_id
    q = sup.fiat_deposit_quote(
        amount_usd=50.0, payment_method="card",
        destination_darwin_id=did, actor="alice@x",
    )

    def ev(event_id: str, quote_id: str) -> dict:
        return {
            "id": event_id,
            "type": "payment_intent.succeeded",
            "data": {"object": {"id": quote_id,
                                "metadata": {"destination_darwin_id": did}}},
        }

    pre = (await sup.status(did)).balance_usdc
    assert sup.handle_stripe_webhook(ev("evt_1", q.quote_id))["status"] == "confirmed"
    post = (await sup.status(did)).balance_usdc
    assert post > pre
    # Layer 1: same event id → duplicate, no money effect.
    assert sup.handle_stripe_webhook(ev("evt_1", q.quote_id))["status"] == "duplicate"
    assert (await sup.status(did)).balance_usdc == post
    # Layer 2: new event id, same already-consumed intent → skipped.
    assert sup.handle_stripe_webhook(ev("evt_2", q.quote_id))["status"] == "skipped"
    assert (await sup.status(did)).balance_usdc == post
    await sup.shutdown(kill_running=True)

    # Restart on the same DB → the event ledger survives; evt_1 still dups.
    sup2 = Supervisor(fiat_ramp=StubFiatRamp(), persistence_path=db)
    assert sup2.handle_stripe_webhook(ev("evt_1", q.quote_id))["status"] == "duplicate"
    await sup2.shutdown(kill_running=True)
