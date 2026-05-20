"""Tests for the SDK-builder factories (validation only; the real SDK
imports are skipped via importorskip)."""

from __future__ import annotations

import pytest


def test_solana_builder_requires_rpc_url():
    pytest.importorskip("solana")
    pytest.importorskip("solders")
    from darwin_platform.adapters.solana_sdk import build_solana_chain_client
    with pytest.raises(ValueError, match="rpc_url"):
        build_solana_chain_client(rpc_url="", private_key_hex="dead")


def test_solana_builder_requires_private_key():
    pytest.importorskip("solana")
    pytest.importorskip("solders")
    from darwin_platform.adapters.solana_sdk import build_solana_chain_client
    with pytest.raises(ValueError, match="private_key_hex"):
        build_solana_chain_client(
            rpc_url="https://api.devnet.solana.com", private_key_hex="",
        )


def test_stellar_builder_requires_horizon_url():
    pytest.importorskip("stellar_sdk")
    from darwin_platform.adapters.stellar_sdk import build_stellar_chain_client
    with pytest.raises(ValueError, match="horizon_url"):
        build_stellar_chain_client(horizon_url="", secret="S" * 56)


def test_stellar_builder_requires_secret():
    pytest.importorskip("stellar_sdk")
    from darwin_platform.adapters.stellar_sdk import build_stellar_chain_client
    with pytest.raises(ValueError, match="secret"):
        build_stellar_chain_client(
            horizon_url="https://horizon-testnet.stellar.org", secret="",
        )


def test_solana_module_import_without_sdk():
    """The solana_sdk module imports fine without the real SDK installed;
    the SDK import is deferred to build_solana_chain_client."""
    # This test only confirms the module-level import works.
    from darwin_platform.adapters import solana_sdk  # noqa: F401


def test_stellar_module_import_without_sdk():
    from darwin_platform.adapters import stellar_sdk  # noqa: F401


# ---------------------------------------------------------------------------
# Real-SDK integration: with the SDKs actually installed, the builders
# must produce a working ChainClient (no network calls — we hit only
# construction + static methods like address()).
# ---------------------------------------------------------------------------

def test_solana_builder_constructs_real_chain_client():
    pytest.importorskip("solana")
    pytest.importorskip("solders")
    from solders.keypair import Keypair as SolKeypair
    from darwin_platform.adapters.solana_sdk import build_solana_chain_client
    raw = bytes(SolKeypair())  # valid 64-byte Ed25519 keypair
    client = build_solana_chain_client(
        rpc_url="https://api.devnet.solana.com",
        private_key_hex=raw.hex(),
        cluster="devnet",
    )
    addr = client.address()
    assert isinstance(addr, str)
    assert len(addr) >= 32  # base58, 32-44 chars


def test_solana_builder_strips_0x_prefix():
    pytest.importorskip("solana")
    pytest.importorskip("solders")
    from solders.keypair import Keypair as SolKeypair
    from darwin_platform.adapters.solana_sdk import build_solana_chain_client
    raw = bytes(SolKeypair())
    a = build_solana_chain_client(
        rpc_url="https://api.devnet.solana.com",
        private_key_hex=raw.hex(),
        cluster="devnet",
    )
    b = build_solana_chain_client(
        rpc_url="https://api.devnet.solana.com",
        private_key_hex="0x" + raw.hex(),
        cluster="devnet",
    )
    assert a.address() == b.address()


def test_stellar_builder_constructs_real_chain_client():
    pytest.importorskip("stellar_sdk")
    from stellar_sdk import Keypair
    from darwin_platform.adapters.stellar_sdk import build_stellar_chain_client
    kp = Keypair.random()
    client = build_stellar_chain_client(
        horizon_url="https://horizon-testnet.stellar.org",
        secret=kp.secret,
        network="testnet",
    )
    addr = client.address()
    assert isinstance(addr, str)
    assert addr.startswith("G")
    assert len(addr) == 56


def test_stellar_builder_distinguishes_public_vs_testnet():
    pytest.importorskip("stellar_sdk")
    from stellar_sdk import Keypair, Network
    from darwin_platform.adapters.stellar_sdk import build_stellar_chain_client
    kp = Keypair.random()
    pub = build_stellar_chain_client(
        horizon_url="https://horizon.stellar.org",
        secret=kp.secret, network="public",
    )
    test = build_stellar_chain_client(
        horizon_url="https://horizon-testnet.stellar.org",
        secret=kp.secret, network="testnet",
    )
    assert (
        pub._server._network  # noqa: SLF001
        == Network.PUBLIC_NETWORK_PASSPHRASE
    )
    assert (
        test._server._network  # noqa: SLF001
        == Network.TESTNET_NETWORK_PASSPHRASE
    )
