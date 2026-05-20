"""Real wallet adapter (docs/engineering/17).

Tests run against a stub :class:`ChainClient` so the suite stays offline
and free. The stub mirrors the real chain's authoritative-balance
semantics: ``transfer`` decrements the balance, raises on insufficient
funds, returns a synthetic tx hash."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from darwin_dna.constitution import OwnerSoftCaps
from darwin_dna.hyperparameters import HARD_CAPS
from darwin_platform.adapters import ChainClient, RealWallet, WalletError


@dataclass
class _StubChain:
    """Test double for the on-chain bits. The supervisor's billing layer
    reconciles against ``balance_usdc``; ``transfer`` is the
    authoritative move."""

    balance: float = 100.0
    address_: str = "0x" + "ab" * 20
    transfers: list[dict] = field(default_factory=list)
    fail_next: int = 0  # if >0, raise the first N calls then go through

    def address(self) -> str:
        return self.address_

    def balance_usdc(self) -> float:
        return float(self.balance)

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        if self.fail_next > 0:
            self.fail_next -= 1
            raise RuntimeError("simulated chain failure")
        if amount_usdc > self.balance + 1e-9:
            raise RuntimeError("insufficient on-chain funds")
        self.balance -= amount_usdc
        tx_hash = f"0xtx{len(self.transfers):08x}"
        self.transfers.append({
            "to": to, "amount_usdc": amount_usdc, "memo": memo,
            "tx_hash": tx_hash,
        })
        return {"tx_hash": tx_hash}


def _vclock(start: float = 1_700_000_000.0):
    """Controllable clock; ``advance(s)`` moves it forward."""
    t = [start]

    def now() -> float:
        return t[0]

    def advance(s: float) -> None:
        t[0] += float(s)

    now.advance = advance  # type: ignore[attr-defined]
    return now


# -- construction --------------------------------------------------------
def test_real_wallet_requires_chain_client():
    with pytest.raises(TypeError, match="ChainClient"):
        RealWallet("not a chain", clock=_vclock())  # type: ignore[arg-type]


def test_balance_comes_from_chain_not_local():
    chain = _StubChain(balance=42.0)
    w = RealWallet(chain, clock=_vclock())
    assert w.balance_usdc() == pytest.approx(42.0)
    chain.balance = 17.0
    assert w.balance_usdc() == pytest.approx(17.0)


def test_address_passes_through_to_chain():
    chain = _StubChain(address_="0xdeadbeef")
    w = RealWallet(chain, clock=_vclock())
    assert w.address() == "0xdeadbeef"


# -- pay submits to chain + updates bookkeeping --------------------------
def test_pay_submits_to_chain_and_updates_bookkeeping():
    chain = _StubChain(balance=100.0)
    w = RealWallet(chain, clock=_vclock())
    out = w.pay(to="0xrecipient", amount_usdc=10.0, memo="api.fee")
    assert chain.balance == pytest.approx(90.0)
    assert chain.transfers[0]["to"] == "0xrecipient"
    assert out["tx_hash"].startswith("0xtx")
    assert w.spent_today_usdc() == pytest.approx(10.0)
    assert w.spent_lifetime_usdc() == pytest.approx(10.0)
    assert w.recent_spend_usdc() == (10.0,)
    # Movement appears in the local ledger so perceive can journal it.
    assert any("debit" in mv and mv["debit"]["amount_usdc"] == 10.0
               for mv in w.movements())


def test_pay_chain_failure_does_not_update_bookkeeping():
    """Critical invariant: if the chain transfer fails, NONE of the
    per-day / per-lifetime / recent-spend counters move. Otherwise the
    platform would think the Darwin spent money that didn't leave the
    wallet."""
    chain = _StubChain(balance=100.0, fail_next=1)
    w = RealWallet(chain, clock=_vclock())
    with pytest.raises(WalletError, match="chain transfer failed"):
        w.pay(to="0xrecipient", amount_usdc=10.0, memo="api.fee")
    assert chain.balance == pytest.approx(100.0)
    assert w.spent_today_usdc() == 0.0
    assert w.spent_lifetime_usdc() == 0.0
    assert w.recent_spend_usdc() == ()


def test_pay_refused_by_per_action_cap():
    chain = _StubChain(balance=1_000.0)
    w = RealWallet(chain, clock=_vclock())
    w.set_policy(per_action_cap_usdc=5.0)
    with pytest.raises(WalletError, match="hard cap"):
        w.pay(to="x", amount_usdc=10.0, memo="too big")
    assert chain.balance == pytest.approx(1_000.0)  # not debited


def test_pay_refused_by_per_day_cap():
    chain = _StubChain(balance=1_000.0)
    w = RealWallet(chain, clock=_vclock())
    w.set_policy(per_day_cap_usdc=20.0)
    w.pay(to="x", amount_usdc=15.0, memo="ok")
    with pytest.raises(WalletError, match="day cap"):
        w.pay(to="x", amount_usdc=10.0, memo="over day")


def test_pay_refused_by_per_lifetime_cap():
    chain = _StubChain(balance=1_000.0)
    w = RealWallet(chain, clock=_vclock())
    w.set_policy(per_lifetime_cap_usdc=30.0)
    w.pay(to="x", amount_usdc=20.0, memo="ok")
    with pytest.raises(WalletError, match="lifetime cap"):
        w.pay(to="x", amount_usdc=20.0, memo="over lifetime")


def test_pay_refused_when_chain_balance_short():
    chain = _StubChain(balance=5.0)
    w = RealWallet(chain, clock=_vclock())
    with pytest.raises(WalletError, match="insufficient on-chain"):
        w.pay(to="x", amount_usdc=10.0, memo="short")


# -- metered debit (gateway billing) -------------------------------------
def test_debit_logs_metered_kind_for_billing_attribution():
    """Platform billing's metered sweep looks for kind=='metered' rows.
    The adapter's gateway debit must use that kind so attribution works."""
    chain = _StubChain(balance=100.0)
    w = RealWallet(chain, clock=_vclock())
    w.debit(0.10, "llm:reasoning")
    metered = [mv for mv in w.movements()
               if "debit" in mv and mv["debit"]["kind"] == "metered"]
    assert len(metered) == 1
    assert metered[0]["debit"]["to"] == "platform.gateway"


# -- credit (incoming revenue) -------------------------------------------
def test_credit_updates_revenue_clock_and_logs_movement():
    chain = _StubChain(balance=100.0)
    clock = _vclock(start=1_000.0)
    w = RealWallet(chain, clock=clock)
    clock.advance(60)  # 1 minute since wallet init
    assert w.seconds_since_revenue() == pytest.approx(60.0)

    w.credit(5.0, "affiliate.payout")
    assert w.seconds_since_revenue() == 0.0
    credits = [mv for mv in w.movements() if "credit" in mv]
    assert len(credits) == 1
    assert credits[0]["credit"]["from"] == "affiliate.payout"


def test_credit_refuses_negative():
    w = RealWallet(_StubChain(), clock=_vclock())
    with pytest.raises(WalletError, match="negative credit"):
        w.credit(-1.0, "x")


# -- harvest -------------------------------------------------------------
def test_harvest_to_owner_runs_two_transfers_and_logs_both():
    chain = _StubChain(balance=200.0)
    w = RealWallet(chain, clock=_vclock())
    out = w.harvest_to_owner(
        destination="bank://owner", net_usdc=80.0, fee_usdc=20.0,
    )
    assert out["gross_usdc"] == pytest.approx(100.0)
    assert chain.balance == pytest.approx(100.0)
    # Two transfers landed on the chain.
    assert [t["to"] for t in chain.transfers] == ["bank://owner", "platform.fee"]
    harvest_rows = [mv["harvest"] for mv in w.movements() if "harvest" in mv]
    kinds = {r["kind"] for r in harvest_rows}
    assert kinds == {"owner_net", "platform_fee"}


def test_harvest_refused_when_short():
    w = RealWallet(_StubChain(balance=50.0), clock=_vclock())
    with pytest.raises(WalletError, match="exceeds on-chain"):
        w.harvest_to_owner(destination="x", net_usdc=80.0, fee_usdc=20.0)


def test_harvest_refuses_negative_amounts():
    w = RealWallet(_StubChain(balance=200.0), clock=_vclock())
    with pytest.raises(WalletError, match="non-negative"):
        w.harvest_to_owner(destination="x", net_usdc=-1.0, fee_usdc=0.0)


def test_harvest_refuses_zero():
    w = RealWallet(_StubChain(balance=200.0), clock=_vclock())
    with pytest.raises(WalletError, match="zero"):
        w.harvest_to_owner(destination="x", net_usdc=0.0, fee_usdc=0.0)


# -- owner soft caps -----------------------------------------------------
def test_owner_soft_caps_none_at_defaults():
    w = RealWallet(_StubChain(), clock=_vclock())
    assert w.owner_soft_caps() is None


def test_owner_soft_caps_returned_when_tightened():
    w = RealWallet(_StubChain(), clock=_vclock())
    w.set_policy(per_action_cap_usdc=5.0, per_day_cap_usdc=10.0)
    caps = w.owner_soft_caps()
    assert isinstance(caps, OwnerSoftCaps)
    assert caps.per_action_usdc == pytest.approx(5.0)
    assert caps.per_day_usdc == pytest.approx(10.0)
    assert caps.per_lifetime_usdc is None


def test_policy_clamps_to_hard_caps():
    w = RealWallet(_StubChain(), clock=_vclock())
    pol = w.set_policy(per_action_cap_usdc=HARD_CAPS.per_action_usdc * 10)
    assert pol["per_action_cap_usdc"] == HARD_CAPS.per_action_usdc


# -- reproduction transfer ----------------------------------------------
def test_transfer_out_for_child_submits_real_transfer():
    chain = _StubChain(balance=100.0)
    w = RealWallet(chain, clock=_vclock())
    w.transfer_out_for_child(25.0)
    assert chain.balance == pytest.approx(75.0)
    rows = [mv for mv in w.movements() if "reproduction_transfer" in mv]
    assert len(rows) == 1
    assert rows[0]["reproduction_transfer"]["amount_usdc"] == pytest.approx(25.0)


def test_transfer_out_for_child_chain_failure_propagates():
    chain = _StubChain(balance=100.0, fail_next=1)
    w = RealWallet(chain, clock=_vclock())
    with pytest.raises(WalletError, match="reproduction transfer failed"):
        w.transfer_out_for_child(25.0)
    assert chain.balance == pytest.approx(100.0)


# -- supervisor swap-in: RealWallet works as the genome's WalletPort -----
def test_real_wallet_satisfies_genome_payments_facilitator() -> None:
    """The genome's payments_facilitator declares a WalletPort Protocol
    with exactly the methods the genome calls. Confirm the real adapter
    satisfies the runtime check."""
    from darwin_dna.neurons.payments_facilitator import WalletPort

    w = RealWallet(_StubChain(), clock=_vclock())
    assert isinstance(w, WalletPort)


def test_real_wallet_satisfies_supervisor_billing_attribution() -> None:
    """The supervisor's billing sweep reads ``movements()`` for kind ==
    'metered' entries. RealWallet's debit() must produce them."""
    from darwin_platform import BillingPolicy, BillingService

    chain = _StubChain(balance=100.0)
    w = RealWallet(chain, clock=_vclock())
    w.debit(1.10, "llm:reasoning")  # $1.10 charged, $0.10 platform revenue
    # Run the billing attribution offline to confirm the shape feeds
    # through unchanged.
    bs = BillingService(BillingPolicy(infrastructure_markup_rate=0.10))
    last = [mv for mv in w.movements() if "debit" in mv][-1]["debit"]
    assert last["kind"] == "metered"
    provider, revenue = bs.policy.revenue_from_metered(last["amount_usdc"])
    assert provider == pytest.approx(1.00)
    assert revenue == pytest.approx(0.10)
