"""RealWallet off-chain metering accrual + chain-derived movement feed.

A stub chain + reader model the real thing: transfers we sign land as
Transfer logs a block later, anyone else's inbound transfer likewise. Every
check reconciles the wallet's feed through the genome's own ledger, the way
``perceive`` does each heartbeat.
"""

from __future__ import annotations

import pytest

from darwin_dna.machinery.ledger import Ledger, treasury_delta
from darwin_platform.adapters.wallet import RealWallet, WalletError

ME = "0x" + "a" * 40
FEE = "0x" + "f" * 40
OWNER = "0x" + "b" * 40
PAYER = "0x" + "c" * 40


class _Chain:
    """Signs for ME; every transfer becomes a log in the next block."""

    def __init__(self, opening: float) -> None:
        self.block = 100
        self.logs: list[dict] = []
        self.balances = {self.block: opening}
        self.n = 0

    def _land(self, frm: str, to: str, amount: float) -> str:
        self.n += 1
        tx = f"0x{self.n:064x}"
        self.block += 1
        prev = self.balances[max(self.balances)]
        delta = (amount if to == ME else 0.0) - (amount if frm == ME else 0.0)
        self.balances[self.block] = prev + delta
        self.logs.append({"tx_hash": tx, "block": self.block, "log_index": 0,
                          "from": frm, "to": to, "amount_usdc": amount})
        return tx

    # ChainClient
    def address(self) -> str:
        return ME

    def balance_usdc(self) -> float:
        return self.balances[max(self.balances)]

    def transfer(self, *, to: str, amount_usdc: float, memo: str) -> dict:
        return {"tx_hash": self._land(ME, to, amount_usdc)}

    # UsdcReader
    def head(self) -> int:
        return self.block

    def balance_at(self, block: int) -> float:
        return self.balances[max(b for b in self.balances if b <= block)]

    def transfers(self, lo: int, hi: int) -> list[dict]:
        return [lg for lg in self.logs if lo <= lg["block"] <= hi]


def _wallet(chain: _Chain, path=None) -> RealWallet:
    return RealWallet(chain, clock=lambda: 1_000.0, reader=chain,
                      fee_address=FEE, state_path=path, confirmations=0)


def _reconciles(w: RealWallet, opening: float) -> None:
    feed = w.movements()
    led = Ledger("d")
    led.seed_opening(opening, ts=0.0)
    led.journal_new(feed, now=1.0)
    led.reconcile(w.balance_usdc(), 1e-9)
    assert opening + sum(treasury_delta(mv) for mv in feed) == pytest.approx(
        w.balance_usdc()
    )


def test_metering_accrues_then_one_settlement_without_drift() -> None:
    chain = _Chain(100.0)
    w = _wallet(chain)
    for _ in range(3):
        w.debit(0.5, "llm:reasoning")
    assert chain.logs == []  # nothing on-chain per call
    assert w.balance_usdc() == pytest.approx(98.5)
    assert w.unsettled_usdc == pytest.approx(1.5)
    _reconciles(w, 100.0)

    assert w.settle(min_usdc=1.0)["amount_usdc"] == pytest.approx(1.5)
    assert w.settle(min_usdc=1.0) is None  # one in flight at a time
    n = len(w.movements())  # sync sees the settlement: no new row
    assert w.unsettled_usdc == 0.0
    assert w.balance_usdc() == pytest.approx(98.5)
    assert len(w.movements()) == n
    _reconciles(w, 100.0)


def test_inbound_is_a_deposit_unless_labelled_revenue() -> None:
    chain = _Chain(10.0)
    w = _wallet(chain)
    w.movements()  # first sync pins the opening block
    chain._land(PAYER, ME, 5.0)
    paid = chain._land(PAYER, ME, 2.0)
    w.credit(2.0, "x402:order-1", tx_hash=paid)
    credits = [mv["credit"] for mv in w.movements() if "credit" in mv]
    assert [c["from"] for c in credits] == [f"deposit:{PAYER}", "x402:order-1"]
    _reconciles(w, 10.0)
    with pytest.raises(WalletError, match="tx hash"):
        w.credit(1.0, "x402:unbacked")


def test_harvest_is_one_transfer_and_fee_accrues() -> None:
    chain = _Chain(50.0)
    w = _wallet(chain)
    w.movements()
    w.harvest_to_owner(destination=OWNER, net_usdc=8.0, fee_usdc=2.0)
    assert [lg["to"] for lg in chain.logs] == [OWNER]
    assert w.unsettled_usdc == pytest.approx(2.0)
    kinds = [mv["harvest"]["kind"] for mv in w.movements() if "harvest" in mv]
    assert sorted(kinds) == ["owner_net", "platform_fee"]
    assert w.balance_usdc() == pytest.approx(40.0)
    _reconciles(w, 50.0)


def test_unknown_outbound_is_booked_not_drift() -> None:
    chain = _Chain(20.0)
    w = _wallet(chain)
    w.hint_grace_s = 0.0  # don't wait for a registration that never comes
    w.movements()
    chain._land(ME, PAYER, 3.0)  # e.g. a key used outside the platform
    assert any("unattributed_out" in mv for mv in w.movements())
    _reconciles(w, 20.0)


def test_state_survives_restart_and_a_torn_tail(tmp_path) -> None:
    path = tmp_path / "wallet.jsonl"
    chain = _Chain(30.0)
    w = _wallet(chain, path)
    w.movements()
    w.debit(1.25, "llm:fast")
    chain._land(PAYER, ME, 4.0)
    w.movements()
    w.debit(0.25, "llm:fast")
    with path.open("a") as f:
        f.write('{"mvs": [{"debit"')  # crash mid-write

    again = _wallet(chain, path)
    assert again.movements() == w.movements()
    assert again.unsettled_usdc == pytest.approx(1.5)
    assert again.spent_lifetime_usdc() == pytest.approx(1.5)
    assert again.balance_usdc() == pytest.approx(w.balance_usdc())
    _reconciles(again, 30.0)


def test_outbound_waits_for_its_late_hint_instead_of_booking_unattributed() -> None:
    """The platform signs for a pod's watch wallet and registers the row a
    moment later; a sync in between must hold, not mis-book the transfer."""
    clock = [1_000.0]
    chain = _Chain(40.0)
    w = RealWallet(chain, clock=lambda: clock[0], reader=chain,
                   fee_address=FEE, confirmations=0)
    w.movements()
    tx = chain._land(ME, OWNER, 5.0)  # signed elsewhere; no hint yet
    feed = w.movements()
    assert not any("unattributed_out" in mv for mv in feed)
    assert w.balance_usdc() == pytest.approx(40.0)  # held before the tx
    w.expect(tx, {"harvest": {"destination": OWNER, "amount_usdc": 5.0,
                              "kind": "owner_net", "ts": 1.0}})
    assert any("harvest" in mv for mv in w.movements())
    assert w.balance_usdc() == pytest.approx(35.0)
    _reconciles(w, 40.0)

    chain._land(ME, PAYER, 1.0)  # never hinted: booked after the grace
    w.movements()
    clock[0] += w.hint_grace_s + 1
    assert any("unattributed_out" in mv for mv in w.movements())
    _reconciles(w, 40.0)


def test_gas_tank_tops_up_only_when_low() -> None:
    """CDP accounts are EOAs: before the platform signs a USDC transfer for a
    Darwin, its account must hold gas. The tank sends a signed top-up only
    below the floor."""
    pytest.importorskip("eth_account")
    from eth_account import Account
    from eth_account._utils.legacy_transactions import Transaction

    from darwin_platform.adapters.web3_chain import GasTank

    sent: list[bytes] = []

    class _Eth:
        account = Account
        gas_price = 10**7
        block_number = 10

        def __init__(self) -> None:
            self.balances = {OWNER.lower(): 0, PAYER.lower(): 10**18}

        def get_balance(self, address):
            return self.balances[address.lower()]

        def get_transaction_count(self, _addr):
            return 7

        def send_raw_transaction(self, raw):
            sent.append(bytes(raw))
            return b"\x11" * 32

        def get_transaction_receipt(self, _h):
            return {"status": 1, "blockNumber": 10}

    class _W3:
        eth = _Eth()

    key = "0x" + "4" * 64
    tank = GasTank(_W3(), private_key=key, chain_id=84532, topup_eth=0.0002)
    assert tank.ensure(PAYER) is None and sent == []  # already has gas
    assert tank.ensure(OWNER) == "0x" + "11" * 32
    tx = Transaction.from_bytes(sent[0])
    assert tx.to.hex().lower() == OWNER[2:].lower()
    assert tx.value == 2 * 10**14
    assert Account.recover_transaction(sent[0]) == tank.address


def test_expected_payment_is_revenue_even_when_its_tx_arrives_first() -> None:
    """An x402 settlement's tx hash only exists after settling; labelling the
    expected payment by payer + amount beforehand classifies it as revenue
    however the sync races."""
    chain = _Chain(5.0)
    w = _wallet(chain)
    w.movements()
    w.credit(1.25, "x402:order-9", payer=PAYER)
    chain._land(PAYER, ME, 1.25)
    chain._land(PAYER, ME, 3.0)  # an unrelated top-up stays a deposit
    sources = [mv["credit"]["from"] for mv in w.movements() if "credit" in mv]
    assert sources == ["x402:order-9", f"deposit:{PAYER}"]
    _reconciles(w, 5.0)
