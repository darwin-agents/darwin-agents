"""Server side of x402 v2 (the storefront that sells a Darwin's service).

Wire format (coinbase/x402 ``specs/transports-v2/http.md``): a 402 carries a
base64 JSON ``PaymentRequired`` in the ``PAYMENT-REQUIRED`` header; the buyer
retries with a base64 JSON ``PaymentPayload`` in ``PAYMENT-SIGNATURE``; the
paid response carries the base64 JSON settlement in ``PAYMENT-RESPONSE``. We
never touch the buyer's signature ourselves: a facilitator ``/verify``s the
payload against OUR requirements and later ``/settle``s it on-chain, paying
the Darwin's own address (``payTo``).
"""

from __future__ import annotations

import base64
import json
from typing import Any, Callable

PAYMENT_REQUIRED = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE = "PAYMENT-RESPONSE"

# Base USDC: CAIP-2 network, contract, and its EIP-712 domain (name/version,
# read from the contracts) that the "exact" scheme's authorization is signed
# against.
_BASE_USDC = {
    "testnet": ("eip155:84532", "0x036CbD53842c5426634e7929541eC2318f3dCF7e", "USDC"),
    "mainnet": ("eip155:8453", "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913", "USD Coin"),
}


def b64_json(obj: Any) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def from_b64_json(value: str) -> Any:
    return json.loads(base64.b64decode(value))


def payment_required(
    *, network: str, pay_to: str, price_usdc: float, url: str, description: str,
    max_timeout_seconds: int = 600,
) -> dict[str, Any]:
    """The ``PaymentRequired`` object for one USDC price on Base."""
    caip2, asset, name = _BASE_USDC[network]
    return {
        "x402Version": 2,
        "resource": {"url": url, "description": description, "mimeType": "application/json"},
        "accepts": [{
            "scheme": "exact",
            "network": caip2,
            "amount": str(int(round(price_usdc * 1_000_000))),
            "asset": asset,
            "payTo": pay_to,
            "maxTimeoutSeconds": int(max_timeout_seconds),
            "extra": {"name": name, "version": "2"},
        }],
    }


class Facilitator:
    """``/verify`` + ``/settle`` against an x402 facilitator (x402.org on
    testnet; Coinbase's CDP facilitator on mainnet, which needs auth headers
    — ``auth_headers()`` returns ``{"verify": {...}, "settle": {...}}`` like
    ``cdp.x402.create_cdp_auth_headers``)."""

    def __init__(
        self, url: str, *, client: Any = None,
        auth_headers: Callable[[], dict[str, dict[str, str]]] | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        import httpx

        self._url = url.rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=timeout_s)
        self._auth = auth_headers

    async def _post(self, op: str, payload: dict, requirements: dict) -> dict[str, Any]:
        headers = (self._auth() or {}).get(op, {}) if self._auth else {}
        r = await self._client.post(f"{self._url}/{op}", json={
            "x402Version": 2,
            "paymentPayload": payload,
            "paymentRequirements": requirements,
        }, headers=headers)
        r.raise_for_status()
        return r.json()

    async def verify(self, payload: dict, requirements: dict) -> dict[str, Any]:
        return await self._post("verify", payload, requirements)

    async def settle(self, payload: dict, requirements: dict) -> dict[str, Any]:
        return await self._post("settle", payload, requirements)
