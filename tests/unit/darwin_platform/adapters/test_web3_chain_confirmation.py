"""Unit tests for Web3ChainClient.wait_for_confirmation (docs/engineering/30).

The block-depth gate is what stops RealWallet from crediting its ledger before
a mainnet transfer is final. We drive it with a stub web3 so no network is
touched: a receipt that reaches the requested depth confirms, a reverted
receipt raises, and a never-mined tx times out (all three are "do not credit"
or "credit" signals RealWallet relies on).
"""

from __future__ import annotations

import pytest

from darwin_platform.adapters.web3_chain import Web3ChainClient

_ADDR = "0x" + "ab" * 20
_USDC = "0x" + "cd" * 20


class _StubEth:
    def __init__(self, *, receipt, block_number):
        self._receipt = receipt
        self.block_number = block_number
        self.calls = 0

    def contract(self, *, address, abi):  # constructor binds the USDC contract
        return object()

    def get_transaction_receipt(self, tx_hash):
        self.calls += 1
        if self._receipt is None:
            raise ValueError("not mined")
        return self._receipt


class _StubW3:
    def __init__(self, eth):
        self.eth = eth


def _client(*, receipt, block_number):
    eth = _StubEth(receipt=receipt, block_number=block_number)
    client = Web3ChainClient(
        web3_factory=lambda: _StubW3(eth),
        wallet_address=_ADDR,
        private_key="0x" + "11" * 32,
        usdc_contract=_USDC,
        chain_id=8453,
    )
    return client


def test_confirms_when_depth_reached():
    client = _client(
        receipt={"status": 1, "blockNumber": 100}, block_number=102,
    )
    out = client.wait_for_confirmation(
        "0xabc", timeout_s=5.0, min_confirmations=3,
    )
    assert out["confirmations"] == 3  # 102 - 100 + 1
    assert out["block_number"] == 100
    assert out["status"] == 1


def test_reverted_receipt_raises():
    client = _client(
        receipt={"status": 0, "blockNumber": 100}, block_number=110,
    )
    with pytest.raises(RuntimeError, match="reverted"):
        client.wait_for_confirmation("0xabc", timeout_s=5.0)


def test_unmined_tx_times_out():
    client = _client(receipt=None, block_number=100)
    with pytest.raises(TimeoutError, match="not confirmed"):
        client.wait_for_confirmation("0xabc", timeout_s=0.3, min_confirmations=1)


def test_insufficient_depth_times_out():
    # Receipt exists at block 100 but only 1 confirmation deep; asking for 6
    # must time out rather than confirm prematurely.
    client = _client(
        receipt={"status": 1, "blockNumber": 100}, block_number=100,
    )
    with pytest.raises(TimeoutError):
        client.wait_for_confirmation("0xabc", timeout_s=0.3, min_confirmations=6)
