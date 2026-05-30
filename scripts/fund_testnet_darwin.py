"""One-off: fund a Darwin's CDP wallet from the Base Sepolia faucet.

Resolves the Darwin's on-chain address by the same coerced account name the
supervisor uses (``_coerce_cdp_account_name(f"darwin-{id}")``), then requests
testnet ETH (gas) and USDC from the CDP faucet. Read-only against the running
platform — talks to CDP directly with the credentials in the environment.

Usage:  set -a; . ./.env; set +a; uv run --all-extras python \
          scripts/fund_testnet_darwin.py <darwin_id> [--usdc-only|--eth-only]
"""
from __future__ import annotations

import asyncio
import os
import sys

from cdp import CdpClient

from darwin_platform.adapters.coinbase_cdp import _coerce_cdp_account_name

NETWORK = "base-sepolia"


async def _main(darwin_id: str, tokens: list[str]) -> int:
    name = _coerce_cdp_account_name(f"darwin-{darwin_id}")
    print(f"darwin_id   = {darwin_id}")
    print(f"cdp account = {name}")
    async with CdpClient(
        api_key_id=os.environ["CDP_API_KEY_ID"],
        api_key_secret=os.environ["CDP_API_KEY_SECRET"],
        wallet_secret=os.environ.get("CDP_WALLET_SECRET"),
    ) as client:
        account = await client.evm.get_or_create_account(name=name)
        print(f"address     = {account.address}")
        for token in tokens:
            try:
                tx = await client.evm.request_faucet(
                    address=account.address, network=NETWORK, token=token,
                )
                print(f"  faucet {token:5} -> tx {tx}")
            except Exception as exc:  # noqa: BLE001 - operator-facing tool
                print(f"  faucet {token:5} -> FAILED: {exc}")
        # Settle, then report the on-chain USDC balance CDP sees.
        await asyncio.sleep(8)
        balances = await client.evm.list_token_balances(
            address=account.address, network=NETWORK,
        )
        print("token balances:")
        for b in balances.balances:
            sym = getattr(b.token, "symbol", None) or b.token.contract_address
            print(f"  {sym}: {b.amount.amount} (decimals={b.amount.decimals})")
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = {a for a in sys.argv[1:] if a.startswith("--")}
    if not args:
        print(__doc__)
        sys.exit(2)
    toks = ["eth", "usdc"]
    if "--usdc-only" in flags:
        toks = ["usdc"]
    elif "--eth-only" in flags:
        toks = ["eth"]
    sys.exit(asyncio.run(_main(args[0], toks)))
