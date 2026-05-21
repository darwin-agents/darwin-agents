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
