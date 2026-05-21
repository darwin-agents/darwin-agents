"""Phase 1 mainnet + on-chain confirmation tests for RealWallet (doc 21).

Confirms the new ``network`` + ``wait_for_confirmation`` constructor
arguments work as documented:

- Unknown network raises ValueError at construction (typo guard).
- Mainnet awareness is exposed via ``network`` + ``is_mainnet``.
- When ``wait_for_confirmation=True`` and the chain client implements
  ``wait_for_confirmation``, every successful debit waits for it.
- A confirmation timeout surfaces as WalletError (bookkeeping is NOT
  updated — same shape as a chain transfer failure).
- A chain client without the optional method silently keeps working
  (backwards-compat for stub chains).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from darwin_platform.adapters.wallet import (
    KNOWN_NETWORKS,
    MAINNET_NETWORKS,
    NETWORK_BASE_MAINNET,
    NETWORK_BASE_SEPOLIA,
    RealWallet,
    WalletError,
)


def _vclock(start: float = 1_700_000_000.0):
    t = [start]

    def now() -> float:
        return t[0]

    return now


@dataclass
class _Chain:
    balance: float = 100.0
    transfers: list[dict] = field(default_factory=list)
    confirmations: list[dict] = field(default_factory=list)
    confirm_raises: Exception | None = None
    has_wait: bool = True

    def address(self) -> str:
        return "0x" + "ab" * 20

    def balance_usdc(self) -> float:
        return float(self.balance)

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        self.balance -= amount_usdc
        h = f"0xtx{len(self.transfers):08x}"
        self.transfers.append({
            "to": to, "amount_usdc": amount_usdc, "memo": memo, "tx_hash": h,
        })
        return {"tx_hash": h}

    def wait_for_confirmation(self, tx_hash, *, timeout_s=60.0,
                              min_confirmations=1):
        if not self.has_wait:
            raise AttributeError("not implemented")
        if self.confirm_raises is not None:
            raise self.confirm_raises
        self.confirmations.append({
            "tx_hash": tx_hash, "timeout_s": timeout_s,
            "min_confirmations": min_confirmations,
        })
        return {"tx_hash": tx_hash, "block": 12345, "status": "confirmed"}


# ---------------------------------------------------------------------------
# Construction guards
# ---------------------------------------------------------------------------

def test_unknown_network_raises():
    with pytest.raises(ValueError, match="unknown network"):
        RealWallet(_Chain(), clock=_vclock(), network="solana-mainnet")


def test_default_is_testnet():
    w = RealWallet(_Chain(), clock=_vclock())
    assert w.network == NETWORK_BASE_SEPOLIA
    assert w.is_mainnet is False


def test_mainnet_flag_is_set():
    w = RealWallet(_Chain(), clock=_vclock(), network=NETWORK_BASE_MAINNET)
    assert w.network == NETWORK_BASE_MAINNET
    assert w.is_mainnet is True


def test_known_networks_includes_mainnet_alias():
    assert NETWORK_BASE_MAINNET in KNOWN_NETWORKS
    assert NETWORK_BASE_MAINNET in MAINNET_NETWORKS


# ---------------------------------------------------------------------------
# Confirmation behaviour
# ---------------------------------------------------------------------------

def test_pay_waits_for_confirmation_when_enabled():
    chain = _Chain()
    w = RealWallet(
        chain, clock=_vclock(),
        network=NETWORK_BASE_MAINNET, wait_for_confirmation=True,
        min_confirmations=3, confirmation_timeout_s=15.0,
    )
    out = w.pay(to="0xrecipient", amount_usdc=5.0, memo="invoice")
    assert out["tx_hash"].startswith("0xtx")
    assert chain.confirmations[0]["min_confirmations"] == 3
    assert chain.confirmations[0]["timeout_s"] == 15.0
    assert out["confirmation"]["status"] == "confirmed"


def test_pay_skips_confirmation_when_disabled():
    chain = _Chain()
    w = RealWallet(
        chain, clock=_vclock(),
        network=NETWORK_BASE_MAINNET, wait_for_confirmation=False,
    )
    out = w.pay(to="0xrecipient", amount_usdc=5.0, memo="invoice")
    assert chain.confirmations == []
    assert "confirmation" not in out


def test_pay_silently_skips_when_chain_lacks_method():
    """A bare ChainClient (no wait method) preserves backwards-compat:
    the wallet doesn't refuse the transfer, it just records no
    confirmation. Use a distinct class with no method to avoid mutating
    the test-double class itself."""

    @dataclass
    class _BareChain:
        balance: float = 100.0
        transfers: list[dict] = field(default_factory=list)

        def address(self) -> str:
            return "0x" + "ab" * 20

        def balance_usdc(self) -> float:
            return float(self.balance)

        def transfer(self, *, to, amount_usdc, memo):
            self.balance -= amount_usdc
            h = f"0xtx{len(self.transfers):08x}"
            self.transfers.append({"tx_hash": h})
            return {"tx_hash": h}

    chain = _BareChain()
    w = RealWallet(
        chain, clock=_vclock(),
        network=NETWORK_BASE_MAINNET, wait_for_confirmation=True,
    )
    out = w.pay(to="0xrecipient", amount_usdc=2.0, memo="x")
    assert "confirmation" not in out


def test_confirmation_failure_surfaces_as_wallet_error():
    chain = _Chain(confirm_raises=TimeoutError("rpc timeout"))
    w = RealWallet(
        chain, clock=_vclock(),
        network=NETWORK_BASE_MAINNET, wait_for_confirmation=True,
    )
    with pytest.raises(WalletError, match="confirmation"):
        w.pay(to="0xrecipient", amount_usdc=2.0, memo="x")
