"""Tests for multi-chain wallet routing (doc 17)."""

from __future__ import annotations

import pytest

from darwin_platform.adapters.multichain import (
    ChainNetwork,
    ChainRoutingError,
    MultiChainRouter,
    SolanaChainClient,
    StellarChainClient,
    detect_network,
)
from darwin_platform.adapters.wallet import ChainClient


# ---------------------------------------------------------------------------
# Address detection
# ---------------------------------------------------------------------------

def test_detect_evm():
    assert detect_network("0x" + "a" * 40) == ChainNetwork.BASE


def test_detect_stellar():
    assert detect_network("G" + "A" * 55) == ChainNetwork.STELLAR


def test_detect_solana():
    assert detect_network("8aBCdefghijklmnopqrstuvwxyz1234567890ABCD") == ChainNetwork.SOLANA


def test_detect_unknown():
    assert detect_network("") is None
    assert detect_network("0xshort") is None


# ---------------------------------------------------------------------------
# Stub ChainClient + router routing
# ---------------------------------------------------------------------------

class _StubClient:
    """ChainClient-conforming stub. Tracks calls; configurable balance + failure."""

    def __init__(self, *, addr, balance=100.0, fail=False):
        self._addr = addr
        self._balance = balance
        self._fail = fail
        self.calls = []

    def address(self): return self._addr
    def balance_usdc(self): return self._balance
    def transfer(self, *, to, amount_usdc, memo):
        if self._fail:
            raise RuntimeError("chain down")
        self.calls.append({"to": to, "amount": amount_usdc, "memo": memo})
        self._balance -= amount_usdc
        return {"tx_hash": f"0x{self._addr[-6:]}-{len(self.calls)}"}


def test_chainclient_protocol_satisfied():
    assert isinstance(_StubClient(addr="0x" + "1" * 40), ChainClient)


def test_router_picks_chain_by_address():
    base = _StubClient(addr="0x" + "b" * 40, balance=200.0)
    solana = _StubClient(addr="So1ana" + "x" * 38, balance=200.0)
    stellar = _StubClient(addr="G" + "S" * 55, balance=200.0)
    router = MultiChainRouter(clients={
        ChainNetwork.BASE: base,
        ChainNetwork.SOLANA: solana,
        ChainNetwork.STELLAR: stellar,
    })
    # EVM recipient → base used.
    r = router.transfer(to="0x" + "9" * 40, amount_usdc=10, memo="m")
    assert r["chain"] == "base"
    assert len(base.calls) == 1


def test_router_falls_through_when_primary_down():
    base = _StubClient(addr="0xa" + "0" * 39, fail=True, balance=200.0)
    solana = _StubClient(addr="So1ana" + "x" * 38, balance=200.0)
    router = MultiChainRouter(
        clients={ChainNetwork.BASE: base, ChainNetwork.SOLANA: solana},
        preferred=[ChainNetwork.BASE, ChainNetwork.SOLANA],
    )
    # Recipient looks like base; base raises; router falls through to solana.
    r = router.transfer(to="0x" + "9" * 40, amount_usdc=10, memo="m")
    assert r["chain"] == "solana"
    assert len(solana.calls) == 1


def test_router_skips_chain_with_insufficient_liquidity():
    base = _StubClient(addr="0xa" + "0" * 39, balance=1.0)  # too small
    solana = _StubClient(addr="So" + "y" * 41, balance=500.0)
    router = MultiChainRouter(
        clients={ChainNetwork.BASE: base, ChainNetwork.SOLANA: solana},
        preferred=[ChainNetwork.BASE, ChainNetwork.SOLANA],
    )
    r = router.transfer(to="0x" + "9" * 40, amount_usdc=50, memo="m")
    # Base couldn't cover; solana took it.
    assert r["chain"] == "solana"
    assert len(base.calls) == 0


def test_router_raises_when_no_chain_has_liquidity():
    base = _StubClient(addr="0xa" + "0" * 39, balance=1.0)
    solana = _StubClient(addr="So" + "y" * 41, balance=1.0)
    router = MultiChainRouter(clients={
        ChainNetwork.BASE: base, ChainNetwork.SOLANA: solana,
    })
    with pytest.raises(ChainRoutingError):
        router.transfer(to="0x" + "9" * 40, amount_usdc=100, memo="m")


def test_router_balance_sums_across_chains():
    a = _StubClient(addr="0x" + "a" * 40, balance=100.0)
    b = _StubClient(addr="So" + "x" * 41, balance=50.0)
    router = MultiChainRouter(clients={
        ChainNetwork.BASE: a, ChainNetwork.SOLANA: b,
    })
    assert router.balance_usdc() == 150.0


def test_empty_clients_refused():
    with pytest.raises(ValueError):
        MultiChainRouter(clients={})


# ---------------------------------------------------------------------------
# SolanaChainClient + StellarChainClient construction guards
# ---------------------------------------------------------------------------

def test_solana_requires_rpc_and_keypair():
    with pytest.raises(ValueError):
        SolanaChainClient(rpc_client=None, keypair=object())


def test_stellar_requires_server_and_keypair():
    with pytest.raises(ValueError):
        StellarChainClient(stellar_server=None, keypair=object())


def test_solana_transfer_delegates_to_rpc_transfer_usdc():
    class _Kp:
        public_key = "So1anaAddress"
    class _RPC:
        def transfer_usdc(self, *, to, amount, memo):
            return {"tx_hash": "0xsol"}
    c = SolanaChainClient(rpc_client=_RPC(), keypair=_Kp())
    result = c.transfer(to="So" + "y" * 41, amount_usdc=10, memo="m")
    assert result["tx_hash"] == "0xsol"


def test_stellar_transfer_delegates_to_server_transfer_usdc():
    class _Kp:
        public_key = "GA" + "B" * 54
    class _Server:
        def transfer_usdc(self, *, to, amount, memo, issuer):
            return {"tx_hash": "0xstellar"}
    c = StellarChainClient(stellar_server=_Server(), keypair=_Kp())
    r = c.transfer(to="G" + "X" * 55, amount_usdc=10, memo="m")
    assert r["tx_hash"] == "0xstellar"
