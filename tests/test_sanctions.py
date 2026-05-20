"""Sanctions / OFAC screening (docs/engineering/17). Covers the screener
contracts, the wallet integrations (FakeWallet + RealWallet), and the
supervisor-level fleet install."""

from __future__ import annotations

import time

import pytest

from darwin_platform.sanctions import (
    AllowAllScreener,
    CachedScreener,
    LocalDenylistScreener,
    SanctionsScreener,
    ScreeningResult,
)


# ---------------------------------------------------------------------------
# Screener primitives
# ---------------------------------------------------------------------------

def test_allow_all_screener_passes_everything():
    s = AllowAllScreener()
    r = s.check("0xanyaddress")
    assert r.allowed is True
    assert r.source == "bypass"


def test_local_denylist_blocks_exact_match():
    s = LocalDenylistScreener(denylist={"0xDEADBEEF"})
    r = s.check("0xdeadbeef")  # case-insensitive
    assert r.allowed is False
    assert "denylist" in (r.reason or "")


def test_local_denylist_blocks_prefix():
    s = LocalDenylistScreener(
        denylist=set(), denied_prefixes=("0xbad",)
    )
    r = s.check("0xBAD0011")
    assert r.allowed is False
    assert "prefix" in (r.reason or "")


def test_local_denylist_allows_symbolic_addresses():
    """Internal symbolic recipients (platform.fee, child.wallet,
    platform.gateway) must bypass screening — they don't represent
    real wallet addresses."""
    s = LocalDenylistScreener(denylist={"platform.fee"})  # operator-set
    # Despite being on denylist, symbolic addresses are allowed.
    r = s.check("platform.fee")
    assert r.allowed is True


def test_cached_screener_caches_verdicts():
    calls = []

    class _Counting:
        def check(self, address):
            calls.append(address)
            return ScreeningResult(allowed=True, source="local")

    s = CachedScreener(_Counting(), ttl_seconds=60)
    s.check("0xabc")
    s.check("0xabc")
    s.check("0xabc")
    assert len(calls) == 1


def test_cached_screener_respects_ttl():
    now = [1000.0]
    calls = []

    class _Counting:
        def check(self, address):
            calls.append(address)
            return ScreeningResult(allowed=True, source="local")

    s = CachedScreener(_Counting(), ttl_seconds=10, clock=lambda: now[0])
    s.check("0xabc")
    now[0] = 1020.0
    s.check("0xabc")
    assert len(calls) == 2


def test_screener_protocol_satisfied_by_default_impls():
    assert isinstance(AllowAllScreener(), SanctionsScreener)
    assert isinstance(LocalDenylistScreener(denylist=set()), SanctionsScreener)
    assert isinstance(
        CachedScreener(AllowAllScreener()), SanctionsScreener
    )


# ---------------------------------------------------------------------------
# FakeWallet integration
# ---------------------------------------------------------------------------

def test_fake_wallet_default_no_screening():
    from darwin_fakes.fake_wallet import FakeWallet

    w = FakeWallet(opening_usdc=100, clock=time.time)
    # No screener: any recipient succeeds (modulo balance).
    result = w.pay(to="0xdeadbeef", amount_usdc=1.0, memo="test")
    assert result["amount_usdc"] == 1.0


def test_fake_wallet_screener_blocks_payment():
    from darwin_fakes.fake_wallet import FakeWallet
    from darwin_fakes.fake_wallet import WalletError

    w = FakeWallet(opening_usdc=100, clock=time.time)
    w.set_sanctions_screener(
        LocalDenylistScreener(denylist={"0xdeadbeef"})
    )
    with pytest.raises(WalletError, match="sanctions"):
        w.pay(to="0xDEADBEEF", amount_usdc=1.0, memo="test")
    # Refused: balance unchanged.
    assert w.balance_usdc() == 100.0


def test_fake_wallet_screener_blocks_harvest():
    from darwin_fakes.fake_wallet import FakeWallet, WalletError

    w = FakeWallet(opening_usdc=100, clock=time.time)
    w.set_sanctions_screener(
        LocalDenylistScreener(denylist={"0xbadowner"})
    )
    with pytest.raises(WalletError, match="sanctions"):
        w.harvest_to_owner(destination="0xbadowner", net_usdc=10, fee_usdc=2)


def test_fake_wallet_screener_allows_platform_fee():
    """The platform.fee symbolic address must pass even when the
    operator's denylist includes it (we'd never deny ourselves)."""
    from darwin_fakes.fake_wallet import FakeWallet

    w = FakeWallet(opening_usdc=100, clock=time.time)
    w.set_sanctions_screener(LocalDenylistScreener(denylist=set()))
    # Harvest to a normal destination — the platform.fee leg is symbolic
    # and must not raise.
    result = w.harvest_to_owner(
        destination="0xgoodowner", net_usdc=10, fee_usdc=2,
    )
    assert result["gross_usdc"] == 12.0


# ---------------------------------------------------------------------------
# RealWallet integration
# ---------------------------------------------------------------------------

class _StubChain:
    def __init__(self, balance=100.0):
        self._bal = balance
        self.transfers = []

    def address(self): return "0xself"
    def balance_usdc(self): return self._bal

    def transfer(self, *, to, amount_usdc, memo):
        self.transfers.append({"to": to, "amount_usdc": amount_usdc,
                               "memo": memo})
        self._bal -= amount_usdc
        return {"tx_hash": "0x" + "f" * 64}


def test_real_wallet_screening_blocks_chain_submit():
    from darwin_platform.adapters.wallet import RealWallet, WalletError

    chain = _StubChain(balance=100.0)
    w = RealWallet(
        chain=chain, clock=lambda: 1000.0,
        sanctions=LocalDenylistScreener(denylist={"0xdeadbeef"}),
    )
    with pytest.raises(WalletError, match="sanctions"):
        w.pay(to="0xdeadbeef", amount_usdc=5.0, memo="test")
    # The chain was NEVER called — the refusal must happen before submit.
    assert chain.transfers == []


def test_real_wallet_screening_allowed_path_still_works():
    from darwin_platform.adapters.wallet import RealWallet

    chain = _StubChain(balance=100.0)
    w = RealWallet(
        chain=chain, clock=lambda: 1000.0,
        sanctions=LocalDenylistScreener(denylist={"0xbad"}),
    )
    w.pay(to="0xgood", amount_usdc=5.0, memo="test")
    assert chain.transfers[0]["to"] == "0xgood"


def test_real_wallet_set_screener_at_runtime():
    from darwin_platform.adapters.wallet import RealWallet, WalletError

    chain = _StubChain(balance=100.0)
    w = RealWallet(chain=chain, clock=lambda: 1000.0)  # no screener
    # First call passes.
    w.pay(to="0xdeadbeef", amount_usdc=1.0, memo="t")
    # Install screener; subsequent calls refused.
    w.set_sanctions_screener(LocalDenylistScreener(denylist={"0xdeadbeef"}))
    with pytest.raises(WalletError, match="sanctions"):
        w.pay(to="0xdeadbeef", amount_usdc=1.0, memo="t")


# ---------------------------------------------------------------------------
# Supervisor wiring
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_supervisor_installs_screener_on_spawn():
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor(
        sanctions_screener=LocalDenylistScreener(denylist={"0xdeadbeef"})
    )
    status = await sup.spawn(opening_usdc=50.0)
    handle = sup._darwins[status.darwin_id]
    # The wallet has the screener installed.
    from darwin_fakes.fake_wallet import FakeWallet, WalletError
    assert isinstance(handle.stack.wallet, FakeWallet)
    with pytest.raises(WalletError, match="sanctions"):
        handle.stack.wallet.pay(
            to="0xdeadbeef", amount_usdc=1.0, memo="t"
        )
    await sup.shutdown(kill_running=True)


@pytest.mark.asyncio
async def test_supervisor_default_no_screener():
    """Default (no screener) preserves the 90-day sim's determinism —
    any address can be paid (subject to balance + caps)."""
    from darwin_platform.supervisor import Supervisor

    sup = Supervisor()
    status = await sup.spawn(opening_usdc=50.0)
    h = sup._darwins[status.darwin_id]
    # FakeWallet's _sanctions is None by default.
    assert h.stack.wallet._sanctions is None
    await sup.shutdown(kill_running=True)


# ---------------------------------------------------------------------------
# CLI parser
# ---------------------------------------------------------------------------

def test_cli_accepts_sanctions_denylist_flag():
    from darwin_platform.__main__ import build_parser

    parser = build_parser()
    args = parser.parse_args([
        "serve", "--sanctions-denylist", "/tmp/denylist.json",
    ])
    assert args.sanctions_denylist == "/tmp/denylist.json"
