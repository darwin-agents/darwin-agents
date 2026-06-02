"""Sanctions-list ingestion + CDP wallet-level spend policy (docs 17/24)."""

from __future__ import annotations

import pytest

from darwin_platform.adapters.coinbase_cdp import (
    CdpChainClient,
    CdpSpendPolicy,
    CdpSpendPolicyError,
)
from darwin_platform.payments.sanctions import (
    build_local_screener,
    parse_sanctioned_addresses,
)


# ---------------------------------------------------------------------------
# list ingestion
# ---------------------------------------------------------------------------

def test_parse_plain_address_list():
    text = "\n".join([
        "# OFAC SDN crypto addresses",
        "0x" + "ab" * 20,
        "",
        "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa",  # BTC-ish
    ])
    out = parse_sanctioned_addresses(text)
    assert ("0x" + "ab" * 20) in out
    assert "1a1zp1ep5qgefi2dmptftl5slmv7divfna" in out  # lowercased


def test_parse_ofac_remark_style():
    text = "SDN Entry; Digital Currency Address - ETH 0x" + "cd" * 20
    out = parse_sanctioned_addresses(text)
    assert ("0x" + "cd" * 20) in out


def test_build_local_screener_blocks_ingested_address():
    bad = "0x" + "ef" * 20
    screener = build_local_screener([bad + "\n"])
    assert screener.check(bad).allowed is False
    assert screener.check("0x" + "11" * 20).allowed is True


def test_build_local_screener_merges_extra_and_prefixes():
    screener = build_local_screener(
        ["0x" + "aa" * 20],
        extra=["0x" + "bb" * 20],
        denied_prefixes=("0xdead",),
    )
    assert not screener.check("0x" + "aa" * 20).allowed
    assert not screener.check("0x" + "bb" * 20).allowed
    assert not screener.check("0xdead0000").allowed


# ---------------------------------------------------------------------------
# CDP wallet-level spend policy
# ---------------------------------------------------------------------------

class _FakeAccount:
    def __init__(self):
        self.transfers = []

    def address(self):
        return "0x" + "11" * 20

    def balance_usdc(self):
        return 1000.0

    def transfer_usdc(self, *, to, amount_usdc, memo):
        self.transfers.append((to, amount_usdc))
        return {"transactionHash": "0x" + "99" * 32}


def _client(policy):
    return CdpChainClient(account=_FakeAccount(), spend_policy=policy)


def test_cdp_policy_blocks_over_per_tx():
    c = _client(CdpSpendPolicy(per_tx_usdc=10.0))
    with pytest.raises(CdpSpendPolicyError, match="per-tx"):
        c.transfer(to="0x" + "22" * 20, amount_usdc=25.0, memo="x")
    # The CDP transfer was never called.
    assert c._account.transfers == []


def test_cdp_policy_blocks_over_daily():
    c = _client(CdpSpendPolicy(per_day_usdc=30.0, clock=lambda: 0.0))
    c.transfer(to="0x" + "22" * 20, amount_usdc=20.0, memo="x")
    with pytest.raises(CdpSpendPolicyError, match="daily"):
        c.transfer(to="0x" + "22" * 20, amount_usdc=20.0, memo="x")


def test_cdp_policy_allowlist():
    ok = "0x" + "22" * 20
    c = _client(CdpSpendPolicy(allowlist=[ok]))
    assert c.transfer(to=ok, amount_usdc=5.0, memo="x")["tx_hash"]
    with pytest.raises(CdpSpendPolicyError, match="allowlist"):
        c.transfer(to="0x" + "33" * 20, amount_usdc=5.0, memo="x")


def test_cdp_policy_allows_within_caps():
    c = _client(CdpSpendPolicy(per_tx_usdc=100.0, per_day_usdc=100.0,
                               clock=lambda: 0.0))
    out = c.transfer(to="0x" + "22" * 20, amount_usdc=50.0, memo="x")
    assert out["tx_hash"]
    assert c._account.transfers == [("0x" + "22" * 20, 50.0)]
