"""x402 storefront: anyone can buy a research report from a Darwin, paying
the Darwin's own wallet in USDC. No platform account is needed — the x402
payment is the authorization.

Flow: no ``PAYMENT-SIGNATURE`` → 402 with the requirements. With one, the
facilitator verifies it (before any LLM spend), the order goes to the Darwin,
and only a finished report is settled — a failed one is never charged.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..adapters.x402_facilitator import (
    PAYMENT_REQUIRED,
    PAYMENT_RESPONSE,
    PAYMENT_SIGNATURE,
    b64_json,
    from_b64_json,
)
from ..payments.orders import OrderBookFull
from ..runtime.supervisor import SupervisorError, UnknownDarwin
from .deps import _sup

router = APIRouter(tags=["Store"])

# Inside the requirements' maxTimeoutSeconds (600), so an authorization never
# expires between verify and settle.
ORDER_TIMEOUT_S = 540.0


class ReportOrder(BaseModel):
    topic: str = Field(min_length=3, max_length=300)


async def _offer(request: Request, darwin_id: str) -> dict[str, Any]:
    try:
        return await _sup(request).store_offer(
            darwin_id, url=str(request.url_for("buy_report", darwin_id=darwin_id)),
        )
    except UnknownDarwin as exc:
        raise HTTPException(404, str(exc))
    except SupervisorError as exc:
        raise HTTPException(409, str(exc))


@router.get("/store/{darwin_id}")
async def store_info(darwin_id: str, request: Request) -> dict[str, Any]:
    required = await _offer(request, darwin_id)
    return {
        "darwin_id": darwin_id,
        "service": "research report",
        "price_usdc": _sup(request).store_price_usdc,
        "payment": required,
    }


async def _await_order(request: Request, darwin_id: str, order_id: str) -> dict | None:
    sup = _sup(request)
    deadline = asyncio.get_running_loop().time() + ORDER_TIMEOUT_S
    while asyncio.get_running_loop().time() < deadline:
        order = await sup.store_order(darwin_id, order_id)
        if order is not None and order.get("status") != "pending":
            return order
        await asyncio.sleep(0.5)
    return None


@router.post("/store/{darwin_id}/report", name="buy_report")
async def buy_report(darwin_id: str, request: Request, order: ReportOrder):
    sup = _sup(request)
    required = await _offer(request, darwin_id)
    requirements = required["accepts"][0]
    header = request.headers.get(PAYMENT_SIGNATURE)
    if not header:
        return JSONResponse(required, status_code=402,
                            headers={PAYMENT_REQUIRED: b64_json(required)})
    try:
        payload = from_b64_json(header)
    except Exception:
        raise HTTPException(400, f"malformed {PAYMENT_SIGNATURE} header")
    facilitator = sup.x402_facilitator
    try:
        verified = await facilitator.verify(payload, requirements)
    except Exception as exc:
        raise HTTPException(502, f"payment facilitator unavailable: {exc}")
    if not verified.get("isValid"):
        rejected = {**required, "error": verified.get("invalidReason") or "invalid payment"}
        return JSONResponse(rejected, status_code=402,
                            headers={PAYMENT_REQUIRED: b64_json(rejected)})
    payer = str(verified.get("payer") or (
        (payload.get("payload") or {}).get("authorization") or {}
    ).get("from") or "")
    amount = int(requirements["amount"]) / 1_000_000

    order_id = uuid.uuid4().hex
    try:
        await sup.store_place_order(darwin_id, order_id, order.topic)
    except OrderBookFull:
        raise HTTPException(429, "this Darwin is busy; try again shortly")
    try:
        done = await _await_order(request, darwin_id, order_id)
        if done is None or done.get("status") != "done":
            raise HTTPException(503, "the report could not be produced; you were not charged")
        await sup.store_revenue(darwin_id, phase="expect", order_id=order_id,
                                amount_usdc=amount, payer=payer)
        try:
            settled = await facilitator.settle(payload, requirements)
        except Exception as exc:
            raise HTTPException(502, f"payment facilitator unavailable: {exc}")
        if not settled.get("success"):
            return JSONResponse({"error": settled.get("errorReason") or "settlement failed"},
                                status_code=402,
                                headers={PAYMENT_RESPONSE: b64_json(settled)})
        await sup.store_revenue(darwin_id, phase="book", order_id=order_id,
                                amount_usdc=amount, payer=payer)
        return JSONResponse(
            {"order_id": order_id, "topic": order.topic,
             "report": (done.get("output") or {}).get("text", "")},
            headers={PAYMENT_RESPONSE: b64_json(settled)},
        )
    finally:
        await sup.store_forget(darwin_id, order_id)
