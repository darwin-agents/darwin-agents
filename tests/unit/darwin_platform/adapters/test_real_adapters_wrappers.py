"""Tests for the real-provider wrappers behind the ChainClient + Vault
Protocols (docs/engineering/16, 17).

Both wrappers are exercised against stub clients — no network. The
adapters' job here is *translation*: turn the ChainClient/VaultClient
contract into the underlying SDK's calls. Real-network smoke tests
belong in a separate integration suite (testnet, Vault dev mode).
"""

from __future__ import annotations

import pytest


# -- HvacVaultClient ------------------------------------------------------
class _StubKvV2:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.raises: Exception | None = None

    def create_or_update_secret(self, *, mount_point, path, secret):
        if self.raises:
            raise self.raises
        self.store[path] = secret["value"]

    def read_secret_version(self, *, mount_point, path, raise_on_deleted_version=False):
        if self.raises:
            raise self.raises
        if path not in self.store:
            raise RuntimeError("not found")
        return {"data": {"data": {"value": self.store[path]}}}

    def delete_metadata_and_all_versions(self, *, mount_point, path):
        if self.raises:
            raise self.raises
        self.store.pop(path, None)


class _StubKvNamespace:
    def __init__(self) -> None:
        self.v2 = _StubKvV2()


class _StubSecretsEngines:
    def __init__(self) -> None:
        self.kv = _StubKvNamespace()


class _StubHvac:
    def __init__(self) -> None:
        self.secrets = _StubSecretsEngines()


def _make_hvac():
    from darwin_platform.adapters import HvacVaultClient

    stub = _StubHvac()
    client = HvacVaultClient(client_factory=lambda: stub, mount_point="secret")
    return client, stub


def test_hvac_round_trip_put_get_delete():
    client, stub = _make_hvac()
    client.kv_put("darwins/d1/public/display_name", "Lana")
    assert client.kv_get("darwins/d1/public/display_name") == "Lana"
    client.kv_delete("darwins/d1/public/display_name")
    assert client.kv_get("darwins/d1/public/display_name") is None


def test_hvac_get_fails_closed_on_backend_error():
    """Doc-16-flavored fail-closed: if the backend raises on read, the
    Vault client returns None — the genome's tiered vault treats that
    as 'no such key' and refuses to leak anything."""
    client, stub = _make_hvac()
    stub.secrets.kv.v2.raises = RuntimeError("vault sealed")
    assert client.kv_get("any") is None


def test_hvac_requires_url_and_token_when_no_factory():
    from darwin_platform.adapters import HvacVaultClient

    # url is always required; token is required for the default token auth.
    with pytest.raises(ValueError, match="url is required"):
        HvacVaultClient()
    with pytest.raises(ValueError, match="token is required"):
        HvacVaultClient(url="https://vault.example.com:8200")


def test_hvac_swaps_in_for_RealVaultBackend():
    """The wrapper conforms to the VaultClient Protocol — so a
    :class:`RealVaultBackend` accepts it directly."""
    from darwin_platform.adapters import RealVaultBackend

    client, _ = _make_hvac()
    backend = RealVaultBackend(client)
    backend.put("k", "v")
    assert backend.get("k") == "v"


# -- Web3ChainClient ------------------------------------------------------
class _StubContractFn:
    def __init__(self, returns=None, build=None):
        self.returns = returns
        self.build = build
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self

    def call(self):
        return self.returns

    def build_transaction(self, params):
        return {"params": params, **(self.build or {})}


class _StubContract:
    def __init__(self) -> None:
        self.functions = self
        self.balanceOf = _StubContractFn(returns=15_500_000)  # $15.5 raw
        self.decimals = _StubContractFn(returns=6)
        self.transfer = _StubContractFn(
            build={"to": "USDC", "data": "0xdeadbeef"},
        )


class _StubAccount:
    @staticmethod
    def sign_transaction(txn, private_key):
        class Signed:
            raw_transaction = b"\x01\x02\x03"

        return Signed()


class _StubEth:
    def __init__(self, contract):
        self._contract = contract
        self.account = _StubAccount()
        self.last_sent: bytes | None = None
        self._nonce = 7

    def contract(self, *, address, abi):
        return self._contract

    def get_transaction_count(self, addr):
        return self._nonce

    def send_raw_transaction(self, raw):
        self.last_sent = raw

        class TxHash:
            def hex(self_inner):
                return "0x1122aabbcc"

        return TxHash()

    def wait_for_transaction_receipt(self, tx_hash, timeout=None):
        class Receipt:
            status = 1

        return Receipt()


class _StubWeb3:
    """Mocked web3.Web3 — only the surface Web3ChainClient touches."""

    def __init__(self, contract):
        self.eth = _StubEth(contract)


def _make_web3():
    contract = _StubContract()
    w3 = _StubWeb3(contract)
    return w3, contract


def test_web3_balance_uses_balanceOf_and_decimals():
    from darwin_platform.adapters import (
        USDC_BASE_SEPOLIA,
        Web3ChainClient,
    )

    w3, contract = _make_web3()
    client = Web3ChainClient(
        web3_factory=lambda: w3,
        wallet_address="0x" + "00" * 20,
        private_key="0x" + "11" * 32,
        usdc_contract=USDC_BASE_SEPOLIA,
        chain_id=84_532,
    )
    # 15_500_000 raw / 10^6 = 15.5
    assert client.balance_usdc() == pytest.approx(15.5)


def test_web3_transfer_signs_and_submits():
    from darwin_platform.adapters import (
        USDC_BASE_SEPOLIA,
        Web3ChainClient,
    )

    w3, contract = _make_web3()
    client = Web3ChainClient(
        web3_factory=lambda: w3,
        wallet_address="0x" + "00" * 20,
        private_key="0x" + "11" * 32,
        usdc_contract=USDC_BASE_SEPOLIA,
        chain_id=84_532,
    )
    out = client.transfer(to="0x" + "aa" * 20, amount_usdc=2.5, memo="api.fee")
    assert out["tx_hash"].startswith("0x")
    # USDC has 6 decimals → 2.5 * 1e6 = 2_500_000 raw.
    args, _ = contract.transfer.calls[-1]
    assert args[1] == 2_500_000
    assert w3.eth.last_sent == b"\x01\x02\x03"


def test_web3_swaps_in_for_RealWallet():
    """The wrapper satisfies the ChainClient Protocol — so a
    :class:`RealWallet` accepts it directly."""
    from darwin_platform.adapters import (
        RealWallet,
        USDC_BASE_SEPOLIA,
        Web3ChainClient,
    )

    w3, _ = _make_web3()
    client = Web3ChainClient(
        web3_factory=lambda: w3,
        wallet_address="0x" + "00" * 20,
        private_key="0x" + "11" * 32,
        usdc_contract=USDC_BASE_SEPOLIA,
        chain_id=84_532,
    )
    wallet = RealWallet(client, clock=lambda: 1_000.0)
    assert wallet.balance_usdc() == pytest.approx(15.5)
    # And a real pay() goes through chain.transfer + bookkeeping.
    out = wallet.pay(to="0x" + "aa" * 20, amount_usdc=1.0, memo="x")
    assert out["tx_hash"].startswith("0x")
    assert wallet.spent_lifetime_usdc() == pytest.approx(1.0)
