"""Platform billing (docs/engineering/08 §"Billing model").

Two revenue streams the platform earns:
1. Markup on every metered debit the genome makes (LLM / compute).
2. A configurable fee on every harvest (owner withdrawal).

These tests:
- Pure math: BillingPolicy splits a metered amount into provider/revenue and
  computes harvest fees within bounds.
- Wallet harvest: net + fee both leave the wallet; balance drops; movements
  ledger records the harvest rows.
- Drive-loop: metered debits the FakeLLM emits accumulate as platform
  revenue on the BillingLedger; the cursor prevents double-counting.
- HTTP: POST /harvest works, GET /billing returns the summary, GET
  /platform/billing aggregates the fleet, scope rules enforced.
"""

from __future__ import annotations

import asyncio
import time
from typing import Callable

import pytest

from darwin_fakes.fake_wallet import FakeWallet, WalletError
from darwin_platform import (
    BillingLedger,
    BillingPolicy,
    Supervisor,
    SupervisorError,
)


async def _wait_until(
    pred: Callable[[], bool], *, timeout: float = 5.0, tick: float = 0.01
) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise AssertionError("predicate never became true")
        await asyncio.sleep(tick)


# -- pure BillingPolicy math ----------------------------------------------
def test_revenue_from_metered_at_10pct_markup():
    p = BillingPolicy(infrastructure_markup_rate=0.10)
    provider, revenue = p.revenue_from_metered(1.10)
    # The Darwin paid $1.10 for what cost the platform $1.00 → $0.10 revenue.
    assert provider == pytest.approx(1.00)
    assert revenue == pytest.approx(0.10)


def test_revenue_from_metered_zero_markup():
    p = BillingPolicy(infrastructure_markup_rate=0.0)
    provider, revenue = p.revenue_from_metered(5.0)
    assert provider == 5.0
    assert revenue == 0.0


def test_harvest_fee_at_default_20pct():
    p = BillingPolicy()
    assert p.harvest_fee(100.0) == pytest.approx(20.0)
    assert p.harvest_fee(0.0) == 0.0


def test_harvest_fee_clamps_to_gross():
    p = BillingPolicy(harvest_fee_rate=1.5)  # would be more than gross
    assert p.harvest_fee(100.0) == pytest.approx(100.0)


def test_harvest_fee_rejects_negative_gross():
    p = BillingPolicy()
    assert p.harvest_fee(-5.0) == 0.0


# -- BillingLedger accumulators -------------------------------------------
def test_ledger_add_metered_accumulates():
    p = BillingPolicy(infrastructure_markup_rate=0.10)
    led = BillingLedger(darwin_id="d1")
    led.add_metered(11.0, p)  # provider 10, revenue 1
    led.add_metered(22.0, p)  # provider 20, revenue 2
    s = led.summary()["infrastructure"]
    assert s["gross_usdc"] == pytest.approx(33.0)
    assert s["provider_cost_usdc"] == pytest.approx(30.0)
    assert s["platform_revenue_usdc"] == pytest.approx(3.0)
    assert s["events"] == 2


def test_ledger_records_harvests_with_history():
    led = BillingLedger(darwin_id="d2")
    led.add_harvest(gross_usdc=100.0, fee_usdc=20.0, destination="bank://x", ts=1.0)
    led.add_harvest(gross_usdc=50.0, fee_usdc=10.0, destination="bank://x", ts=2.0)
    s = led.summary()["harvests"]
    assert s["gross_usdc"] == pytest.approx(150.0)
    assert s["net_usdc"] == pytest.approx(120.0)
    assert s["fee_usdc"] == pytest.approx(30.0)
    assert s["events"] == 2
    assert len(s["history_tail"]) == 2


def test_ledger_total_platform_revenue_combines():
    p = BillingPolicy(infrastructure_markup_rate=0.10)
    led = BillingLedger(darwin_id="d3")
    led.add_metered(11.0, p)  # +$1 revenue
    led.add_harvest(gross_usdc=100.0, fee_usdc=20.0, destination="x", ts=1.0)
    s = led.summary()
    assert s["total_platform_revenue_usdc"] == pytest.approx(21.0)


# -- FakeWallet harvest_to_owner ------------------------------------------
def test_wallet_harvest_moves_funds_and_logs():
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 1_000.0)
    result = w.harvest_to_owner(
        destination="bank://owner", net_usdc=80.0, fee_usdc=20.0
    )
    assert result["gross_usdc"] == pytest.approx(100.0)
    assert result["net_usdc"] == pytest.approx(80.0)
    assert result["fee_usdc"] == pytest.approx(20.0)
    assert w.balance_usdc() == pytest.approx(100.0)
    # Both rows on the ledger, identifiable by kind.
    harvest_rows = [
        mv["harvest"] for mv in w.movements() if "harvest" in mv
    ]
    kinds = {r["kind"] for r in harvest_rows}
    assert kinds == {"owner_net", "platform_fee"}


def test_wallet_harvest_refuses_when_short():
    w = FakeWallet(opening_usdc=50.0, clock=lambda: 0.0)
    with pytest.raises(WalletError, match="exceeds balance"):
        w.harvest_to_owner(destination="x", net_usdc=80.0, fee_usdc=20.0)


def test_wallet_harvest_refuses_negative_amounts():
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 0.0)
    with pytest.raises(WalletError, match="non-negative"):
        w.harvest_to_owner(destination="x", net_usdc=-1.0, fee_usdc=0.0)


def test_wallet_harvest_refuses_zero():
    w = FakeWallet(opening_usdc=200.0, clock=lambda: 0.0)
    with pytest.raises(WalletError, match="zero"):
        w.harvest_to_owner(destination="x", net_usdc=0.0, fee_usdc=0.0)


# -- Supervisor.harvest ---------------------------------------------------
async def test_supervisor_harvest_records_billing() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        # Let it tick so the wallet has settled into baseline.
        await _wait_until(lambda: h.runtime.state.tick >= 3)
        # Earn enough that the whole draw is profit (the fee is on profit);
        # block reproduction so the Darwin doesn't spend it on a child.
        await sup.set_reproduction_block(status.darwin_id, blocked=True)
        h.stack.wallet.credit(150.0, "affiliate_network")

        result = await sup.harvest(
            status.darwin_id, amount_usdc=100.0, destination="bank://owner"
        )
        # Default policy = 20% fee.
        assert result["gross_usdc"] == pytest.approx(100.0)
        assert result["fee_usdc"] == pytest.approx(20.0)
        assert result["net_usdc"] == pytest.approx(80.0)

        summary = await sup.billing(status.darwin_id)
        assert summary["harvests"]["fee_usdc"] == pytest.approx(20.0)
        assert summary["total_platform_revenue_usdc"] >= 20.0
    finally:
        await sup.shutdown()


async def test_supervisor_harvest_validates_inputs() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        with pytest.raises(SupervisorError, match="positive"):
            await sup.harvest(status.darwin_id, amount_usdc=0.0,
                              destination="x")
        with pytest.raises(SupervisorError, match="destination"):
            await sup.harvest(status.darwin_id, amount_usdc=10.0,
                              destination="")
        with pytest.raises(SupervisorError, match="wallet"):
            # Larger than balance.
            await sup.harvest(status.darwin_id, amount_usdc=10_000.0,
                              destination="x")
    finally:
        await sup.shutdown()


async def test_supervisor_harvest_custom_policy() -> None:
    sup = Supervisor(
        heartbeat_seconds=0.0,
        billing_policy=BillingPolicy(harvest_fee_rate=0.05),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await _wait_until(lambda: h.runtime.state.tick >= 1)
        await sup.set_reproduction_block(status.darwin_id, blocked=True)
        h.stack.wallet.credit(150.0, "affiliate_network")
        result = await sup.harvest(
            status.darwin_id, amount_usdc=100.0, destination="bank://owner"
        )
        assert result["fee_usdc"] == pytest.approx(5.0)
        assert result["net_usdc"] == pytest.approx(95.0)
    finally:
        await sup.shutdown()


async def test_harvest_keeps_darwin_alive_and_fees_profit_only() -> None:
    """Regression: a harvest row used to raise ``LedgerError`` in the
    genome's perceive, killing the drive task while status still said alive.
    A draw of principal pays no fee, and a deposit never counts as profit."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0, escalation_auto="approve")
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        st = h.runtime.state
        await _wait_until(lambda: st.tick >= 3)
        await sup.set_reproduction_block(status.darwin_id, blocked=True)

        # A deposit raises the baseline: no profit, so a draw is fee-free.
        h.stack.wallet.credit(50.0, "deposit:owner-topup")
        t = st.tick
        await _wait_until(lambda: st.tick > t + 1)
        assert st.balance_usdc - st.profit_baseline_usdc <= 0.0
        principal = await sup.harvest(
            status.darwin_id, amount_usdc=40.0, destination="bank://owner"
        )
        assert principal["fee_usdc"] == 0.0

        # Revenue then a draw of part of it: fee on the profit part only.
        h.stack.wallet.credit(30.0, "affiliate_network")
        t = st.tick
        await _wait_until(lambda: st.tick > t + 1)
        profit = st.balance_usdc - st.profit_baseline_usdc
        assert 0.0 < profit <= 30.0
        mixed = await sup.harvest(
            status.darwin_id, amount_usdc=profit + 10.0,
            destination="bank://owner",
        )
        # (Live balance may sit a few metered cents under the perceive-time
        # one; the tolerance is far below the $2 a fee on the whole draw adds.)
        assert mixed["fee_usdc"] == pytest.approx(0.20 * profit, abs=0.5)

        # The Darwin keeps ticking after both harvests and never drifted.
        t = st.tick
        await _wait_until(lambda: st.tick > t + 3)
        assert st.alive
        assert not h.task.done()
    finally:
        await sup.shutdown()


# -- Drive-loop attribution of metered debits -----------------------------
async def test_metered_debits_accrue_platform_revenue() -> None:
    """A live Darwin doing LLM calls produces metered debits. The
    supervisor sweeps them and accumulates platform revenue under the
    configured markup."""
    sup = Supervisor(
        heartbeat_seconds=0.0,
        billing_policy=BillingPolicy(
            harvest_fee_rate=0.20, infrastructure_markup_rate=0.10
        ),
    )
    try:
        status = await sup.spawn(opening_usdc=200.0)
        # Wait for a few heartbeats — the fake LLM debits the wallet on
        # each call. Plan + reasoning calls in the founding loop should
        # produce metered entries within a few ticks.
        await _wait_until(
            lambda: (
                sup.billing_service.summary(
                    status.darwin_id
                )["infrastructure"]["events"] > 0
            ),
            timeout=5.0,
        )
        summary = await sup.billing(status.darwin_id)
        infra = summary["infrastructure"]
        # The revenue identity holds: revenue = gross - provider_cost.
        assert infra["platform_revenue_usdc"] == pytest.approx(
            infra["gross_usdc"] - infra["provider_cost_usdc"], abs=1e-6
        )
        assert infra["platform_revenue_usdc"] > 0
    finally:
        await sup.shutdown()


async def test_billing_cursor_prevents_double_count() -> None:
    """The supervisor's billing sweep is cursor-based. A re-sweep does not
    re-attribute already-counted entries."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await _wait_until(
            lambda: sup.billing_service.summary(
                status.darwin_id
            )["infrastructure"]["events"] > 0
        )
        before = sup.billing_service.summary(
            status.darwin_id
        )["infrastructure"]
        # Force a redundant sweep from the same cursor; nothing changes.
        sup._observe_billing(h)  # noqa: SLF001
        after = sup.billing_service.summary(
            status.darwin_id
        )["infrastructure"]
        assert before == after
    finally:
        await sup.shutdown()


async def test_harvest_rows_not_counted_as_metered_revenue() -> None:
    """The harvest_to_owner movement uses a ``harvest`` kind, not
    ``metered``. The cursor bump after harvest also guards against the
    sweep treating fee rows as infrastructure pass-through."""
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        status = await sup.spawn(opening_usdc=200.0)
        h = sup._darwins[status.darwin_id]  # noqa: SLF001
        await _wait_until(lambda: h.runtime.state.tick >= 3)
        await sup.set_reproduction_block(status.darwin_id, blocked=True)
        h.stack.wallet.credit(100.0, "affiliate_network")  # fee is on profit
        before = sup.billing_service.summary(
            status.darwin_id
        )["infrastructure"]["platform_revenue_usdc"]
        await sup.harvest(
            status.darwin_id, amount_usdc=50.0, destination="bank://x"
        )
        # Give the drive loop a tick to run; the sweep would have re-run
        # but should have nothing to add (harvest movements aren't metered).
        await asyncio.sleep(0.05)
        after = sup.billing_service.summary(
            status.darwin_id
        )["infrastructure"]["platform_revenue_usdc"]
        # Metered revenue from genuine LLM calls may still grow between
        # the two reads; what we assert is the platform did NOT pick up
        # the $10 harvest fee as part of infrastructure revenue. The
        # harvests bucket carried it instead.
        harvests = await sup.billing(status.darwin_id)
        assert harvests["harvests"]["fee_usdc"] == pytest.approx(10.0)
        # Infrastructure grew at most by a tiny LLM-call amount,
        # certainly not by the $10 fee.
        assert (after - before) < 1.0
    finally:
        await sup.shutdown()


# -- fleet billing --------------------------------------------------------
async def test_fleet_billing_sorts_and_aggregates() -> None:
    sup = Supervisor(heartbeat_seconds=0.0)
    try:
        a = await sup.spawn(opening_usdc=200.0)
        b = await sup.spawn(opening_usdc=200.0)
        # The fee is on profit: give both revenue (and keep them from
        # reproducing on it) before harvesting.
        for d in (a, b):
            h = sup._darwins[d.darwin_id]  # noqa: SLF001
            await _wait_until(lambda h=h: h.runtime.state.tick >= 1)
            await sup.set_reproduction_block(d.darwin_id, blocked=True)
            h.stack.wallet.credit(150.0, "affiliate_network")
        # Harvest different amounts so the two darwins end up with
        # distinct platform revenues — fleet view sorts by total revenue.
        await sup.harvest(a.darwin_id, amount_usdc=100.0, destination="x")
        await sup.harvest(b.darwin_id, amount_usdc=50.0, destination="x")

        fleet = await sup.fleet_billing()
        assert fleet["total"]["darwins"] == 2
        assert fleet["total"]["harvest_fees_usdc"] >= 30.0
        ids = [row["darwin_id"] for row in fleet["per_darwin"]]
        # a's fee was $20, b's was $10 → a sorts first.
        assert ids[0] == a.darwin_id
    finally:
        await sup.shutdown()


# -- HTTP endpoints -------------------------------------------------------
async def test_billing_endpoints_owner_and_operator_scopes() -> None:
    pytest.importorskip("fastapi")
    httpx = pytest.importorskip("httpx")
    from darwin_platform import InMemoryTokenStore, OWNER, PLATFORM_OPERATOR
    from darwin_platform.api import build_app  # type: ignore

    store = InMemoryTokenStore()
    store.issue("owner-tok", scopes={OWNER})
    store.issue("ops-tok", scopes={PLATFORM_OPERATOR})

    sup = Supervisor(heartbeat_seconds=0.0)
    app = build_app(sup, token_store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        try:
            r = await c.post(
                "/darwins",
                json={"opening_usdc": 200.0},
                headers={"Authorization": "Bearer owner-tok"},
            )
            did = r.json()["darwin_id"]
            h = sup._darwins[did]  # noqa: SLF001
            await _wait_until(lambda: h.runtime.state.tick >= 1)
            await sup.set_reproduction_block(did, blocked=True)
            h.stack.wallet.credit(150.0, "affiliate_network")  # fee is on profit

            r = await c.post(
                f"/darwins/{did}/harvest",
                json={"amount_usdc": 100.0, "destination": "bank://owner"},
                headers={"Authorization": "Bearer owner-tok"},
            )
            assert r.status_code == 200, r.text
            assert r.json()["fee_usdc"] == pytest.approx(20.0)

            # Owner can read their own billing.
            r = await c.get(
                f"/darwins/{did}/billing",
                headers={"Authorization": "Bearer owner-tok"},
            )
            assert r.status_code == 200
            assert r.json()["harvests"]["fee_usdc"] == pytest.approx(20.0)

            # Fleet billing rejects owner, allows operator.
            r = await c.get(
                "/platform/billing",
                headers={"Authorization": "Bearer owner-tok"},
            )
            assert r.status_code == 403
            r = await c.get(
                "/platform/billing",
                headers={"Authorization": "Bearer ops-tok"},
            )
            assert r.status_code == 200
            assert r.json()["total"]["harvest_fees_usdc"] >= 20.0

            # Bad inputs → 422 (pydantic) / 409 (supervisor refused).
            r = await c.post(
                f"/darwins/{did}/harvest",
                json={"amount_usdc": -1.0, "destination": "x"},
                headers={"Authorization": "Bearer owner-tok"},
            )
            assert r.status_code == 422
            r = await c.post(
                f"/darwins/{did}/harvest",
                json={"amount_usdc": 999_999.0, "destination": "x"},
                headers={"Authorization": "Bearer owner-tok"},
            )
            assert r.status_code == 409
        finally:
            await sup.shutdown()


async def test_production_harvest_only_to_a_settled_payout_address(tmp_path) -> None:
    """In production a harvest may only go to the owner's payout address,
    and only once it has stood for the cooldown (a hijacked session can't
    redirect funds on the spot)."""
    now = [time.time()]
    sup = Supervisor(
        heartbeat_seconds=0.0, persistence_path=str(tmp_path / "p.db"),
        require_payout_address=True, clock=lambda: now[0],
    )
    good = "0x52908400098527886E0F7030069857D2E4169EE7"
    try:
        did = (await sup.spawn(opening_usdc=100.0, actor="owner")).darwin_id
        with pytest.raises(SupervisorError, match="payout address before"):
            await sup.harvest(did, amount_usdc=1.0, destination=good)
        with pytest.raises(SupervisorError, match="checksummed"):
            await sup.set_payout_address(did, address=good.lower())
        await sup.set_payout_address(did, address=good, actor="owner")
        with pytest.raises(SupervisorError, match="set recently"):
            await sup.harvest(did, amount_usdc=1.0, destination=good)
        now[0] += Supervisor.PAYOUT_COOLDOWN_SECONDS + 1
        with pytest.raises(SupervisorError, match="only go to"):
            await sup.harvest(did, amount_usdc=1.0, destination="0x" + "b" * 40)
        out = await sup.harvest(did, amount_usdc=1.0, destination=good)
        assert out["destination"] == good
    finally:
        await sup.shutdown()
