"""x402 storefront: a buyer pays a Darwin for a report, end to end.

A stub facilitator stands in for x402.org / Coinbase's; everything else is
the real path — the 402 offer, the order reaching the Darwin's paid-report
strategy, ``content.writer`` writing it, settlement, and revenue landing in
the Darwin's own ledger. A payment that doesn't verify, or a report that
can't be produced, is never settled.
"""

from __future__ import annotations

import pytest

httpx = pytest.importorskip("httpx")
pytest.importorskip("fastapi")

from darwin_platform.adapters.x402_facilitator import (  # noqa: E402
    PAYMENT_REQUIRED,
    PAYMENT_RESPONSE,
    PAYMENT_SIGNATURE,
    b64_json,
    from_b64_json,
)
from darwin_platform.api import build_app  # noqa: E402
from darwin_platform.runtime.supervisor import Supervisor  # noqa: E402

BUYER = "0x" + "c" * 40


class _Facilitator:
    def __init__(self) -> None:
        self.verified: list[dict] = []
        self.settled: list[dict] = []

    async def verify(self, payload, requirements):
        self.verified.append(requirements)
        ok = payload.get("payload", {}).get("signature") == "good"
        return {"isValid": ok, "payer": BUYER} if ok else {
            "isValid": False, "invalidReason": "invalid_signature"}

    async def settle(self, payload, requirements):
        self.settled.append(requirements)
        return {"success": True, "transaction": "0x" + "ab" * 32,
                "network": requirements["network"], "payer": BUYER}


def _pay(sig: str) -> dict:
    return {PAYMENT_SIGNATURE: b64_json({"x402Version": 2, "payload": {"signature": sig}})}


@pytest.mark.asyncio
async def test_buyer_pays_darwin_for_a_report():
    fac = _Facilitator()
    sup = Supervisor(heartbeat_seconds=0.0, store_price_usdc=0.5, x402_facilitator=fac)
    app = build_app(sup)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://t", timeout=30) as c:
        did = (await c.post("/darwins", json={"opening_usdc": 10.0})).json()["darwin_id"]
        wallet = sup._darwins[did].stack.wallet  # noqa: SLF001

        # 1. No payment: the x402 offer, paying the Darwin's own wallet.
        r = await c.post(f"/store/{did}/report", json={"topic": "solar panel ROI"})
        assert r.status_code == 402
        req = from_b64_json(r.headers[PAYMENT_REQUIRED])["accepts"][0]
        assert req["payTo"] == wallet.address()
        assert req["amount"] == "500000" and req["network"] == "eip155:84532"

        # 2. A payment that doesn't verify: rejected, never settled.
        r = await c.post(f"/store/{did}/report", json={"topic": "x" * 10},
                         headers=_pay("bad"))
        assert r.status_code == 402 and fac.settled == []

        # 3. A valid payment: the Darwin writes it, then it settles.
        before = wallet.balance_usdc()
        r = await c.post(f"/store/{did}/report", json={"topic": "solar panel ROI"},
                         headers=_pay("good"))
        assert r.status_code == 200, r.text
        assert r.json()["report"].strip()
        assert from_b64_json(r.headers[PAYMENT_RESPONSE])["success"] is True
        assert len(fac.settled) == 1
        sales = [mv["credit"] for mv in wallet.movements()
                 if "credit" in mv and mv["credit"]["from"].startswith("x402:")]
        assert [s["amount_usdc"] for s in sales] == [0.5]
        assert wallet.balance_usdc() > before - 0.5  # revenue in, minus metering

        # 4. A report that can't be produced: 503, still never settled.
        async def failed(_did, _oid):
            return {"status": "failed", "reason": "llm_unavailable"}
        sup.store_order = failed
        r = await c.post(f"/store/{did}/report", json={"topic": "anything else"},
                         headers=_pay("good"))
        assert r.status_code == 503 and len(fac.settled) == 1
