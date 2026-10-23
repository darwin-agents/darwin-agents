"""v1 end-to-end check against a running platform on Base Sepolia.

Walks the money path a real alpha user and buyer exercise, and prints
PASS/FAIL per step:

  1. spawn a Darwin (owner responds to escalations; consent given)
  2. a buyer pays the Darwin for a report over x402 — with the OFFICIAL
     x402 client, so this doubles as a wire-compatibility check
  3. the sale lands in the Darwin's own ledger as revenue
  4. the ledger never drifted (no ``ledger.reconciliation_failed`` event)
  5. the owner sets a payout address and harvests to it
  6. kill sweeps what's left to the payout address

The buyer is a CDP account funded from the Base Sepolia faucet (needs
CDP_API_KEY_ID / CDP_API_KEY_SECRET / CDP_WALLET_SECRET), or any key holding
Sepolia USDC via --buyer-key.

Usage:
  uv run --all-extras --with "x402[evm]" python scripts/e2e_testnet.py \\
      --url https://api.staging.example --token "$OWNER_TOKEN" \\
      --payout-address 0xYourSepoliaAddress
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import time

import httpx

RESULTS: list[tuple[str, bool, str]] = []


def step(name: str, ok: bool, detail: str = "") -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}{' — ' + detail if detail else ''}")
    return ok


def wait(pred, *, timeout: float, every: float = 3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = pred()
        if out:
            return out
        time.sleep(every)
    return None


def _buyer_key(args) -> str:
    if args.buyer_key:
        return args.buyer_key
    from eth_account import Account

    acct = Account.create()

    async def fund():
        import os

        from cdp import CdpClient

        async with CdpClient(
            api_key_id=os.environ["CDP_API_KEY_ID"],
            api_key_secret=os.environ["CDP_API_KEY_SECRET"],
            wallet_secret=os.environ.get("CDP_WALLET_SECRET"),
        ) as cdp:
            await cdp.evm.request_faucet(address=acct.address, network="base-sepolia", token="usdc")

    asyncio.run(fund())
    print(f"buyer {acct.address} funded from the Base Sepolia USDC faucet")
    return acct.key.hex()


def buy_report(api: httpx.Client, did: str, key: str, topic: str) -> httpx.Response:
    from eth_account import Account
    from x402.client import x402Client
    from x402.mechanisms.evm.exact.register import register_exact_evm_client
    from x402.mechanisms.evm.signers import EthAccountSigner
    from x402.schemas import PaymentRequired

    first = api.post(f"/store/{did}/report", json={"topic": topic})
    if first.status_code != 402:
        raise RuntimeError(f"expected 402, got {first.status_code}: {first.text}")
    required = PaymentRequired.model_validate(
        json.loads(base64.b64decode(first.headers["PAYMENT-REQUIRED"]))
    )
    client = register_exact_evm_client(x402Client(), EthAccountSigner(Account.from_key(key)))
    payload = asyncio.run(client.create_payment_payload(required))
    header = base64.b64encode(
        json.dumps(payload.model_dump(by_alias=True, exclude_none=True)).encode()
    ).decode()
    return api.post(f"/store/{did}/report", json={"topic": topic},
                    headers={"PAYMENT-SIGNATURE": header}, timeout=600)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--token", required=True, help="owner bearer token")
    ap.add_argument("--payout-address", required=True, help="checksummed Sepolia address")
    ap.add_argument("--buyer-key", default=None, help="hex key holding Sepolia USDC")
    ap.add_argument("--opening-usdc", type=float, default=1.0)
    args = ap.parse_args()

    api = httpx.Client(base_url=args.url.rstrip("/"), timeout=60,
                       headers={"Authorization": f"Bearer {args.token}"})

    r = api.post("/darwins", json={
        "opening_usdc": args.opening_usdc, "escalation_auto": None,
        "terms_accepted": True, "consent_18_plus": True,
    })
    if not step("spawn", r.status_code == 201, r.text[:200]):
        return 1
    did = r.json()["darwin_id"]

    offer = api.get(f"/store/{did}")
    step("storefront offer", offer.status_code == 200, offer.text[:200])

    r = buy_report(api, did, _buyer_key(args), "the economics of rooftop solar in 2026")
    step("buy a report over x402", r.status_code == 200 and bool(r.json().get("report")),
         f"HTTP {r.status_code}")

    def sale():
        rows = api.get(f"/darwins/{did}/ledger").json()
        rows = rows.get("movements", rows) if isinstance(rows, dict) else rows
        return [m for m in rows if "credit" in m and m["credit"]["from"].startswith("x402:")]
    step("sale booked as revenue", bool(wait(sale, timeout=120)))

    events = api.get(f"/darwins/{did}/events", params={"limit": 1000}).json()
    events = events.get("events", events) if isinstance(events, dict) else events
    drift = [e for e in events if "reconciliation_failed" in str(e.get("kind"))]
    step("no ledger drift", not drift, f"{len(drift)} drift events")

    r = api.put(f"/darwins/{did}/payout-address", json={"address": args.payout_address})
    step("set payout address", r.status_code == 200, r.text[:200])
    r = api.post(f"/darwins/{did}/harvest",
                 json={"amount_usdc": 0.01, "destination": args.payout_address})
    step("harvest to payout address", r.status_code == 200, r.text[:200])

    r = api.post(f"/darwins/{did}/kill", json={"reason": "e2e done"})
    killed = wait(lambda: api.get(f"/darwins/{did}").json().get("killed"), timeout=60)
    step("kill", r.status_code in (200, 202) and bool(killed))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
