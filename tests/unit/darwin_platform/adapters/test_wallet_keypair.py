"""Tests for the local-vault keypair minter + ensure_keypair glue.

The minters themselves are gated on optional SDK extras and skipped when
not installed; the ensure_keypair glue is pure-Python and always runs."""

from __future__ import annotations

import importlib

import pytest

from darwin_fakes.fake_vault import FakeVault
from darwin_platform.adapters.wallet_keypair import (
    SUPPORTED_CHAINS,
    WalletKeypair,
    address_path,
    ensure_keypair,
    secret_path,
)


class _StubMinter:
    """Returns a sequence of pre-canned keypairs so tests can assert on
    exact values without dragging in a real SDK."""

    def __init__(self, chain: str, results: list[WalletKeypair]) -> None:
        self.chain = chain
        self._results = list(results)
        self.calls = 0

    def mint(self) -> WalletKeypair:
        self.calls += 1
        return self._results.pop(0)


def test_ensure_keypair_mints_on_empty_vault_and_seals():
    vault = FakeVault()
    expected = WalletKeypair(chain="solana", secret="deadbeef", address="addr1")
    minter = _StubMinter("solana", [expected])

    kp = ensure_keypair(vault, chain="solana", minter=minter)

    assert kp == expected
    assert minter.calls == 1
    assert vault.get(secret_path("solana")) == "deadbeef"
    assert vault.get(address_path("solana")) == "addr1"


def test_ensure_keypair_is_idempotent_on_resume():
    """The same vault state should yield the same keypair without minting
    again — this is how resume after process restart preserves identity."""
    vault = FakeVault()
    first = WalletKeypair(chain="solana", secret="aaaa", address="addrA")
    second = WalletKeypair(chain="solana", secret="bbbb", address="addrB")
    minter = _StubMinter("solana", [first, second])

    kp1 = ensure_keypair(vault, chain="solana", minter=minter)
    kp2 = ensure_keypair(vault, chain="solana", minter=minter)

    assert kp1 == first
    assert kp2 == first
    assert minter.calls == 1  # mint not called the second time


def test_ensure_keypair_isolates_chains():
    vault = FakeVault()
    sol = _StubMinter("solana", [
        WalletKeypair(chain="solana", secret="sol-sec", address="sol-addr"),
    ])
    stl = _StubMinter("stellar", [
        WalletKeypair(chain="stellar", secret="stl-sec", address="stl-addr"),
    ])

    ensure_keypair(vault, chain="solana", minter=sol)
    ensure_keypair(vault, chain="stellar", minter=stl)

    assert vault.get(secret_path("solana")) == "sol-sec"
    assert vault.get(secret_path("stellar")) == "stl-sec"
    assert vault.get(secret_path("solana")) != vault.get(secret_path("stellar"))


def test_ensure_keypair_rejects_minter_chain_mismatch():
    """A factory bug that pairs a Solana minter with chain='base' must
    not silently store the wrong-shape secret under the base key."""
    vault = FakeVault()
    sol = _StubMinter("solana", [
        WalletKeypair(chain="solana", secret="x", address="y"),
    ])
    with pytest.raises(ValueError, match="minter is for chain"):
        ensure_keypair(vault, chain="base", minter=sol)


def test_ensure_keypair_rejects_unknown_chain():
    vault = FakeVault()
    bogus = _StubMinter("dogecoin", [
        WalletKeypair(chain="dogecoin", secret="x", address="y"),
    ])
    with pytest.raises(ValueError, match="unknown chain"):
        ensure_keypair(vault, chain="dogecoin", minter=bogus)


def test_secret_and_address_paths_under_sensitive_tier():
    """Doc 16: anything under sensitive/ is subject to 90-day retention
    sweep. Pinning the prefix here so a refactor doesn't accidentally
    drop the key into scoped/ and lose the audit treatment."""
    for chain in SUPPORTED_CHAINS:
        assert secret_path(chain).startswith("sensitive/wallet/")
        assert address_path(chain).startswith("sensitive/wallet/")
        assert secret_path(chain) != address_path(chain)


# -- Real-SDK smoke tests --------------------------------------------------
# Skipped unless the optional extra is installed. They only verify that the
# minter returns a non-empty (secret, address) pair — full validity of the
# resulting keys is the chain SDKs' problem.

def _have(module: str) -> bool:
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


@pytest.mark.skipif(
    not _have("solders"), reason="solana extra not installed",
)
def test_solana_minter_produces_distinct_keypairs():
    from darwin_platform.adapters.wallet_keypair import SolanaKeypairMinter

    m = SolanaKeypairMinter()
    a = m.mint()
    b = m.mint()
    assert a.chain == "solana"
    assert a.secret and a.address
    assert a.secret != b.secret
    assert a.address != b.address


@pytest.mark.skipif(
    not _have("stellar_sdk"), reason="stellar extra not installed",
)
def test_stellar_minter_produces_distinct_keypairs():
    from darwin_platform.adapters.wallet_keypair import StellarKeypairMinter

    m = StellarKeypairMinter()
    a = m.mint()
    b = m.mint()
    assert a.chain == "stellar"
    assert a.secret and a.address
    assert a.secret != b.secret
    assert a.address != b.address


@pytest.mark.skipif(
    not _have("eth_account"), reason="web3 extra not installed",
)
def test_evm_minter_produces_distinct_keypairs():
    from darwin_platform.adapters.wallet_keypair import EvmKeypairMinter

    m = EvmKeypairMinter()
    a = m.mint()
    b = m.mint()
    assert a.chain == "base"
    # eth_account's .key.hex() omits the "0x" prefix in recent versions
    # and includes it in older ones; accept either since web3.py's
    # sign_transaction handles both. Address always has 0x.
    assert len(a.secret.removeprefix("0x")) == 64
    assert a.address.startswith("0x")
    assert a.secret != b.secret
    assert a.address != b.address
