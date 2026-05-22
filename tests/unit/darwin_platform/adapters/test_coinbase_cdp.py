"""Coinbase CDP wallet adapter (docs/engineering/05, 17).

CdpChainClient is exercised against a stub CdpAccount — no SDK, no
network. The factory's two error paths (missing credentials, missing
SDK) are checked without the optional cdp-sdk.
"""

from __future__ import annotations

import importlib.util
import types

import pytest

from darwin_platform.adapters.coinbase_cdp import (
    CdpChainClient,
    _AsyncBridge,
    build_cdp_chain_client,
)
from darwin_platform.adapters.wallet import ChainClient


class _StubCdpAccount:
    """A CdpAccount stand-in — records transfers, returns a scripted result."""

    def __init__(self, *, addr="0xCDP", balance=100.0, transfer_result=None):
        self._addr = addr
        self._balance = balance
        self._result = (
            transfer_result if transfer_result is not None
            else {"tx_hash": "0xtxn"}
        )
        self.transfers: list[dict] = []

    def address(self) -> str:
        return self._addr

    def balance_usdc(self) -> float:
        return self._balance

    def transfer_usdc(self, *, to, amount_usdc, memo):
        self.transfers.append(
            {"to": to, "amount_usdc": amount_usdc, "memo": memo}
        )
        return self._result


# -- CdpChainClient --------------------------------------------------------

def test_cdp_chain_client_satisfies_the_chain_client_protocol():
    client = CdpChainClient(account=_StubCdpAccount())
    assert isinstance(client, ChainClient)


def test_cdp_address_and_balance_pass_through():
    client = CdpChainClient(account=_StubCdpAccount(addr="0xABC", balance=42.5))
    assert client.address() == "0xABC"
    assert client.balance_usdc() == 42.5


def test_cdp_transfer_happy_path():
    acct = _StubCdpAccount(transfer_result={"tx_hash": "0xdeadbeef"})
    client = CdpChainClient(account=acct, network="base-sepolia")
    out = client.transfer(to="0xRecipient", amount_usdc=5.0, memo="ad spend")
    assert out["tx_hash"] == "0xdeadbeef"
    assert out["memo"] == "ad spend"
    assert out["network"] == "base-sepolia"
    assert out["provider"] == "coinbase-cdp"
    assert acct.transfers == [
        {"to": "0xRecipient", "amount_usdc": 5.0, "memo": "ad spend"}
    ]


def test_cdp_transfer_rejects_non_positive_amount():
    client = CdpChainClient(account=_StubCdpAccount())
    with pytest.raises(ValueError):
        client.transfer(to="0xR", amount_usdc=0.0, memo="m")
    with pytest.raises(ValueError):
        client.transfer(to="0xR", amount_usdc=-1.0, memo="m")


def test_cdp_transfer_rejects_empty_destination():
    client = CdpChainClient(account=_StubCdpAccount())
    with pytest.raises(ValueError):
        client.transfer(to="", amount_usdc=1.0, memo="m")


def test_cdp_transfer_raises_when_no_tx_hash_returned():
    client = CdpChainClient(account=_StubCdpAccount(transfer_result={}))
    with pytest.raises(RuntimeError):
        client.transfer(to="0xR", amount_usdc=1.0, memo="m")


def test_cdp_requires_an_account():
    with pytest.raises(ValueError):
        CdpChainClient(account=None)


@pytest.mark.parametrize("result,expected", [
    ({"tx_hash": "0xa"}, "0xa"),
    ({"transaction_hash": "0xb"}, "0xb"),
    ({"transactionHash": "0xc"}, "0xc"),
    ("0xbarestring", "0xbarestring"),
    (types.SimpleNamespace(transaction_hash="0xobj"), "0xobj"),
])
def test_cdp_tx_hash_extraction_handles_sdk_shapes(result, expected):
    client = CdpChainClient(account=_StubCdpAccount(transfer_result=result))
    out = client.transfer(to="0xR", amount_usdc=1.0, memo="m")
    assert out["tx_hash"] == expected


# -- async bridge ----------------------------------------------------------

def test_async_bridge_runs_a_coroutine_to_completion():
    bridge = _AsyncBridge()

    async def _double(x):
        return x * 2

    assert bridge.run(_double(21)) == 42


def test_async_bridge_propagates_exceptions():
    bridge = _AsyncBridge()

    async def _boom():
        raise ValueError("from the coroutine")

    with pytest.raises(ValueError, match="from the coroutine"):
        bridge.run(_boom())


# -- factory error paths ---------------------------------------------------

def test_build_cdp_requires_credentials():
    with pytest.raises(ValueError):
        build_cdp_chain_client(api_key_id="", api_key_secret="x")
    with pytest.raises(ValueError):
        build_cdp_chain_client(api_key_id="x", api_key_secret="")


def test_build_cdp_without_the_sdk_gives_a_clear_hint():
    if importlib.util.find_spec("cdp") is not None:
        pytest.skip("cdp-sdk is installed — the ImportError path is moot")
    with pytest.raises(ImportError, match="cdp"):
        build_cdp_chain_client(api_key_id="key-id", api_key_secret="secret")
