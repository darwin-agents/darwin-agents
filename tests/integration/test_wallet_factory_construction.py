"""Regression tests for the per-Darwin wallet factories (docs/engineering/30).

The wallet factories built by ``_build_real_factories`` only fire at Darwin
*spawn*, not at import or at ``serve`` boot — so a wrong constructor call
(e.g. ``Web3ChainClient(rpc_url=..., private_key=...)`` against a keyword-only
constructor that has no ``rpc_url`` and requires ``web3_factory`` /
``wallet_address`` / ``usdc_contract`` / ``chain_id``) raises ``TypeError``
only when a real Darwin is born. These tests actually *invoke* each factory so
that mismatch can never regress silently again.
"""

from __future__ import annotations

import pytest

from darwin_platform.__main__ import _build_real_factories, build_parser
from darwin_platform.adapters.wallet import RealWallet
from darwin_platform.adapters.web3_chain import Web3ChainClient
from darwin_fakes.fake_vault import FakeVault

# A valid 32-byte secp256k1 key (eth_account accepts the 0x-prefixed hex).
_TEST_KEY = "0x" + "11" * 32


def _args(*extra: str):
    return build_parser().parse_args(["serve", *extra])


def _spawn_wallet(factories, *, vault=None):
    """Invoke the wallet factory exactly as the Supervisor does at spawn."""
    return factories["wallet_factory"]("darwin-test", lambda: 0.0, vault)


# ---------------------------------------------------------------------------
# web3 shared-key provider
# ---------------------------------------------------------------------------

def test_web3_factory_constructs_base_wallet_on_testnet():
    factories = _build_real_factories(_args(
        "--wallet-provider", "web3",
        "--chain-networks", "base",
        "--rpc-url", "http://localhost:8545",
        "--wallet-key", _TEST_KEY,
    ))
    wallet = _spawn_wallet(factories)
    assert isinstance(wallet, RealWallet)
    assert isinstance(wallet._chain, Web3ChainClient)
    # Authoritative network switch: testnet by default, no confirmation gate.
    assert wallet.network == "base-sepolia"
    assert wallet.is_mainnet is False


def test_web3_factory_refuses_mainnet_with_shared_key(capsys):
    # A single fleet-shared key signing real funds must be refused on mainnet
    # via the authoritative --network switch (not a brittle RPC substring).
    with pytest.raises(SystemExit):
        _build_real_factories(_args(
            "--wallet-provider", "web3",
            "--chain-networks", "base",
            "--rpc-url", "https://base.llamarpc.com",  # mainnet RPC, no "mainnet" substring
            "--wallet-key", _TEST_KEY,
            "--network", "mainnet",
        ))
    assert "mainnet" in capsys.readouterr().err.lower()


def test_web3_factory_allows_mainnet_lookalike_rpc_on_testnet():
    # Regression: the old substring guard refused any RPC containing "mainnet"
    # even when --network testnet. The authoritative switch must allow it.
    factories = _build_real_factories(_args(
        "--wallet-provider", "web3",
        "--chain-networks", "base",
        "--rpc-url", "https://sepolia.example.com/mainnet-mirror",
        "--wallet-key", _TEST_KEY,
    ))
    wallet = _spawn_wallet(factories)
    assert wallet.network == "base-sepolia"


# ---------------------------------------------------------------------------
# local-vault provider (per-Darwin minted key)
# ---------------------------------------------------------------------------

def test_local_vault_factory_constructs_base_wallet():
    factories = _build_real_factories(_args(
        "--wallet-provider", "local-vault",
        "--chain-networks", "base",
        "--rpc-url", "http://localhost:8545",
    ))
    vault = FakeVault()
    wallet = _spawn_wallet(factories, vault=vault)
    assert isinstance(wallet, RealWallet)
    assert isinstance(wallet._chain, Web3ChainClient)
    assert wallet.network == "base-sepolia"
    # The minted key was sealed in the per-Darwin vault and the wallet address
    # derives from it.
    from eth_account import Account
    sealed = vault.get("sensitive/wallet/base/secret")
    assert sealed is not None
    assert wallet._chain.address() == Account.from_key(sealed).address


def test_local_vault_factory_mainnet_enables_confirmation_gating():
    # local-vault is a doc-blessed mainnet path (per-Darwin keys), so it is NOT
    # refused on mainnet — but it must run the mainnet network with on-chain
    # confirmation gating enabled so the ledger isn't credited before a tx
    # confirms.
    factories = _build_real_factories(_args(
        "--wallet-provider", "local-vault",
        "--chain-networks", "base",
        "--rpc-url", "http://localhost:8545",
        "--network", "mainnet",
    ))
    wallet = _spawn_wallet(factories, vault=FakeVault())
    assert wallet.network == "base-mainnet"
    assert wallet.is_mainnet is True
    assert wallet._wait_for_confirmation is True
